"""Deterministic replay client for recorded Provider acceptance attempts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.agent.models import LLMResponse, Message, ToolDefinition
from app.observability.provider_attempt_recorder import _redact


class ReplayError(RuntimeError):
    """Raised when a replay recording cannot satisfy the next request."""


def _message_payload(message: Message) -> dict[str, Any]:
    result: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.role == "assistant" and message.tool_calls:
        result["tool_calls"] = [{
            "id": item.call_id,
            "type": "function",
            "function": {"name": item.name, "arguments": json.dumps(item.arguments, ensure_ascii=False)},
        } for item in message.tool_calls]
    if message.role == "tool":
        if not message.tool_call_id:
            raise ReplayError("tool_message_missing_call_identity")
        result["tool_call_id"] = message.tool_call_id
    return result


def _tool_payload(tool: ToolDefinition) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": tool.name, "description": tool.description, "parameters": tool.parameters,
    }}


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _mismatch_summary(expected: dict[str, Any], actual: dict[str, Any]) -> str:
    expected_messages = expected.get("messages", [])
    actual_messages = actual.get("messages", [])
    differing = []
    for index in range(max(len(expected_messages), len(actual_messages))):
        expected_message = expected_messages[index] if index < len(expected_messages) else None
        actual_message = actual_messages[index] if index < len(actual_messages) else None
        if expected_message != actual_message:
            differing.append(str(index))
    extra = sorted(set(expected) ^ set(actual))
    return f"messages={','.join(differing) or '-'};extra_keys={','.join(extra) or '-'}"


def _canonicalize_tool_call_ids(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize ephemeral tool identities and legacy-redacted policy booleans."""
    normalized = json.loads(json.dumps(payload))
    identifiers: dict[str, str] = {}

    def restore_policy_booleans(value: Any) -> Any:
        if isinstance(value, dict):
            for key, item in list(value.items()):
                if key == "requires_execution_authorization" and item == "[REDACTED]":
                    value[key] = True
                else:
                    value[key] = restore_policy_booleans(item)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                value[index] = restore_policy_booleans(item)
        elif isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return value
            if isinstance(parsed, (dict, list)):
                return json.dumps(restore_policy_booleans(parsed), ensure_ascii=False)
        return value

    def canonical_id(identifier: str) -> str:
        if identifier not in identifiers:
            identifiers[identifier] = f"tool-call-{len(identifiers) + 1}"
        return identifiers[identifier]

    for message in normalized.get("messages", []):
        if "content" in message:
            content = message["content"]
            try:
                parsed_content = json.loads(content) if isinstance(content, str) else None
            except (json.JSONDecodeError, TypeError):
                parsed_content = None
            if isinstance(parsed_content, (dict, list)):
                message["content"] = json.dumps(restore_policy_booleans(parsed_content), ensure_ascii=False)
        for tool_call in message.get("tool_calls", []):
            identifier = tool_call.get("id")
            if isinstance(identifier, str):
                tool_call["id"] = canonical_id(identifier)
        identifier = message.get("tool_call_id")
        if isinstance(identifier, str):
            message["tool_call_id"] = canonical_id(identifier)
    return normalized


def _request_payload(messages, tools, response_format):
    payload = {
        "model": None,
        "messages": [_message_payload(item) for item in messages],
    }
    if tools:
        payload["tools"] = [_tool_payload(item) for item in tools]
        payload["tool_choice"] = "auto"
        payload["parallel_tool_calls"] = False
    if response_format is not None:
        payload["response_format"] = response_format
    return payload


class ProviderReplayClient:
    """Consume recorded normalized responses without creating a network client."""

    def __init__(self, attempts_path: str | Path):
        self.attempts_path = Path(attempts_path).resolve()
        raw = self.attempts_path.read_bytes()
        self.source_sha256 = hashlib.sha256(raw).hexdigest()
        self._attempts = []
        for line_number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                attempt = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReplayError(f"invalid_attempt_json:{line_number}") from exc
            if not isinstance(attempt, dict):
                raise ReplayError(f"invalid_attempt_record:{line_number}")
            if not isinstance(attempt.get("call_id"), str) or not attempt["call_id"].strip():
                raise ReplayError(f"missing_call_identity:{line_number}")
            payload = attempt.get("request_payload")
            if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
                raise ReplayError(f"missing_request_identity:{line_number}")
            is_http_error = isinstance(attempt.get("http_status"), int) and attempt["http_status"] >= 400
            if not isinstance(attempt.get("response"), dict) and not attempt.get("error_type") and not is_http_error:
                raise ReplayError(f"missing_recorded_outcome:{line_number}")
            self._attempts.append(attempt)
        if not self._attempts:
            raise ReplayError("empty_provider_recording")
        self.calls = 0

    async def complete(self, messages: list[Message], tools: list[ToolDefinition], *, response_format=None):
        if self.calls >= len(self._attempts):
            raise ReplayError("recording_exhausted")
        attempt = self._attempts[self.calls]
        actual = _request_payload(messages, tools, response_format)
        expected = attempt["request_payload"]
        expected = {key: value for key, value in expected.items() if key != "model"}
        actual = {key: value for key, value in actual.items() if key != "model"}
        comparable_expected = _canonicalize_tool_call_ids(expected)
        comparable_actual = _canonicalize_tool_call_ids(actual)
        if _digest(comparable_actual) != _digest(comparable_expected):
            summary = _mismatch_summary(expected, actual)
            error = ReplayError(f"request_mismatch:{attempt['call_id']}:{summary}")
            error.expected_payload = _redact(expected)
            error.actual_payload = _redact(actual)
            error.call_id = attempt["call_id"]
            raise error
        self.calls += 1
        status = attempt.get("http_status")
        if isinstance(status, int) and status >= 400:
            raise ReplayError(f"recorded_http_error:{status}:{attempt['call_id']}")
        if attempt.get("error_type"):
            raise ReplayError(f"recorded_provider_error:{attempt['error_type']}")
        try:
            return LLMResponse.model_validate(attempt["response"])
        except Exception as exc:
            raise ReplayError(f"invalid_recorded_response:{attempt['call_id']}") from exc

    @property
    def exhausted(self) -> bool:
        return self.calls == len(self._attempts)

    @property
    def attempt_count(self) -> int:
        return len(self._attempts)


def write_replay_summary(path: str | Path, *, source_path: str | Path, client: ProviderReplayClient,
                         status: str, error: str | None = None) -> None:
    """Write replay-only provenance separately from any real acceptance summary."""
    destination = Path(path).resolve()
    source = Path(source_path).resolve()
    if destination == source:
        raise ReplayError("replay_summary_must_not_overwrite_source")
    summary = {
        "mode": "replay",
        "status": status,
        "source_recording": str(client.attempts_path),
        "source_sha256": client.source_sha256,
        "recorded_attempts": client.attempt_count,
        "replayed_attempts": client.calls,
        "real_provider_compatibility_claim": False,
    }
    if error:
        summary["error"] = error[:500]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")