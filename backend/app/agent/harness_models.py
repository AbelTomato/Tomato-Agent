from copy import deepcopy
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import ContextState


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Budget(_ContractModel):
    max_loops: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    max_duration_seconds: float = Field(gt=0)
    max_context_tokens: int = Field(gt=0)
    max_response_chars: int = Field(gt=0)


class ToolPolicy(_ContractModel):
    allowed_tools: frozenset[str]
    max_calls: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0)
    max_result_chars: int = Field(gt=0)


class UsageSnapshot(_ContractModel):
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)


class HarnessState(_ContractModel):
    task_id: str | None = None
    structured_state: dict[str, Any] = Field(default_factory=dict)
    context_state: ContextState
    evidence_refs: tuple[str, ...] = ()
    step_index: int = Field(default=0, ge=0)

    @property
    def untrusted_structured_state(self) -> dict[str, Any]:
        """Return dynamic state as data, never as a trusted policy fragment."""

        return deepcopy(self.structured_state)

    @property
    def untrusted_data_label(self) -> Literal["untrusted"]:
        return "untrusted"


class ExecutionResult(_ContractModel):
    status: Literal["completed", "stopped", "failed"]
    output: Any | None = None
    stop_reason: str | None = None
    usage: UsageSnapshot
    tool_calls: int = Field(ge=0)
    duration_ms: float = Field(ge=0)


_SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "credential",
    "password",
    "secret",
    "token",
}


def _contains_sensitive_payload(value: Any, *, key: str | None = None) -> bool:
    if key is not None and key.lower().replace("-", "_") in _SENSITIVE_KEYS:
        return True
    if isinstance(value, BaseException):
        return True
    if isinstance(value, dict):
        return any(
            _contains_sensitive_payload(item, key=str(item_key))
            for item_key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_sensitive_payload(item) for item in value)
    return False


class AuditEvent(_ContractModel):
    event_type: str
    status: Literal["started", "success", "stopped", "failed"]
    payload: dict[str, Any]
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include timezone information")
        return value

    @field_validator("payload")
    @classmethod
    def reject_sensitive_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        if _contains_sensitive_payload(value):
            raise ValueError("audit payload must be sanitized")
        return value