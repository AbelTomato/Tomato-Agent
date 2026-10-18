import json
import re
from pathlib import Path
from typing import Any

_SECRET_KEY = re.compile(r"(?:api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|token|secret|password|credential)", re.I)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s\",}]+")
_NON_SECRET_AUTHORIZATION_FIELDS = {"requires_execution_authorization"}


def _redact(value: Any, key: str = "") -> Any:
    if key.lower() not in _NON_SECRET_AUTHORIZATION_FIELDS and _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _redact(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            redacted = _redact(parsed)
            if redacted != parsed:
                return json.dumps(redacted, ensure_ascii=False)
        return _BEARER.sub("Bearer [REDACTED]", value)
    return value


class ProviderAttemptRecorder:
    def __init__(
        self,
        path: Path,
        attempt: int,
        run_id: str | None,
        *,
        acceptance_id: str | None = None,
        scenario_id: str | None = None,
        call_id: str | None = None,
        step_id: str | None = None,
        operation_id: str | None = None,
    ):
        self.path = path
        self.record: dict[str, Any] = {
            "attempt": attempt,
            "acceptance_id": acceptance_id,
            "scenario_id": scenario_id,
            "run_id": run_id,
            "run_association": "execution_context" if run_id else "unknown",
            "call_id": call_id,
            "step_id": step_id,
            "operation_id": operation_id,
            "correlation_missing": call_id is None,
            "request_payload": None,
            "raw_response_body": None,
            "http_status": None,
        }

    def observe(self, payload: dict[str, Any], response, context: dict[str, Any] | None = None) -> None:
        context = context or {}
        for name in ("acceptance_id", "scenario_id", "run_id", "call_id", "step_id", "operation_id"):
            if context.get(name) is not None:
                self.record[name] = context[name]
        self.record["correlation_missing"] = self.record["call_id"] is None
        self.record["request_payload"] = _redact(payload)
        if response is not None:
            self.record["http_status"] = response.status_code
            self.record["raw_response_body"] = _redact(response.content.decode("utf-8", errors="replace"))
            self.record["raw_response_body_encoding"] = "utf-8-replace"

    def write(self) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(_redact(self.record), ensure_ascii=False) + "\n")