"""Safety wrapper for bounded real-provider evaluation calls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.agent.interfaces import LLMClient
from app.agent.models import LLMResponse, Message, ToolDefinition


class ProviderCallBudgetExceeded(RuntimeError):
    """Raised before a provider request would exceed the configured budget."""


class BudgetedRecordingLLMClient(LLMClient):
    def __init__(
        self,
        client: LLMClient,
        *,
        max_calls: int,
        output_path: Path,
    ) -> None:
        if max_calls <= 0:
            raise ValueError("max_calls must be greater than zero")
        self.client = client
        self.max_calls = max_calls
        self.output_path = output_path
        self.call_count = 0

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
    ) -> LLMResponse:
        if self.call_count >= self.max_calls:
            raise ProviderCallBudgetExceeded(
                f"provider call budget exceeded: maximum of {self.max_calls} calls"
            )

        self.call_count += 1
        try:
            response = await self.client.complete(messages, tools)
        except Exception as exc:
            safe_error = self._safe_error_name(exc)
            self._append_record(
                {
                    "call_index": self.call_count,
                    "status": "error",
                    "error": safe_error,
                    "token_usage": self._token_usage(),
                }
            )
            raise RuntimeError(safe_error) from exc

        self._append_record(
            {
                "call_index": self.call_count,
                "status": "success",
                "response": response.model_dump(mode="json"),
                "token_usage": self._token_usage(),
            }
        )
        return response

    def _token_usage(self) -> dict[str, int] | str:
        usage = getattr(self.client, "last_token_usage", None)
        if not isinstance(usage, dict):
            return "unknown"
        return {
            key: value
            for key, value in usage.items()
            if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
            and isinstance(value, int)
        }

    @staticmethod
    def _safe_error_name(error: Exception) -> str:
        if isinstance(error, RuntimeError) and str(error).startswith(
            "LLM provider request failed: HTTP "
        ):
            return str(error)
        if isinstance(error, TimeoutError):
            return "provider timeout"
        return "provider request failed"

    def _append_record(self, record: dict[str, Any]) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")