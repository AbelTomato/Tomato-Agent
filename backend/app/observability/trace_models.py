"""Versioned, content-free spans emitted by the Agent Runtime."""

import re
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


TRACE_SCHEMA = "runtime-trace/v1"

TraceOperation = Literal[
    "run.execute",
    "model.complete",
    "tool.execute",
    "memory.recall",
    "knowledge.search",
    "validation.run",
]
TraceStatus = Literal["ok", "error", "cancelled", "timeout"]
TraceErrorCategory = Literal[
    "runtime",
    "provider",
    "tool",
    "memory",
    "knowledge",
    "validation",
    "cancelled",
    "timeout",
    "unknown",
]

_ATTRIBUTE_KEYS = frozenset({
    "provider_name",
    "model_name",
    "attempt",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "usage_known",
    "tool_name",
    "result_count",
    "result_chars",
    "retry_count",
    "timeout_ms",
})
_ATTRIBUTE_STRING_LIMIT = 128
_MAX_ATTRIBUTES = 16
_IDENTIFIER_ATTRIBUTE_KEYS = frozenset({"provider_name", "model_name", "tool_name"})
_BOOLEAN_ATTRIBUTE_KEYS = frozenset({"usage_known"})
_NUMERIC_ATTRIBUTE_KEYS = _ATTRIBUTE_KEYS - _IDENTIFIER_ATTRIBUTE_KEYS - _BOOLEAN_ATTRIBUTE_KEYS
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class RuntimeTraceSpan(BaseModel):
    """A completed Runtime span. Attributes are deliberately not extensible."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["runtime-trace/v1"]
    event_id: UUID
    runtime_instance_id: UUID
    sequence: int = Field(gt=0)
    trace_id: UUID
    span_id: UUID
    parent_span_id: UUID | None
    run_id: UUID
    operation: TraceOperation
    started_at: datetime
    duration_ms: float = Field(ge=0, allow_inf_nan=False)
    status: TraceStatus
    error_code: str | None
    error_category: TraceErrorCategory | None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("started_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("started_at must be timezone-aware UTC")
        return value

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str | None) -> str | None:
        if value is not None and (
            not value
            or len(value) > 64
            or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in value)
        ):
            raise ValueError("error_code must be a stable lowercase code")
        return value

    @field_validator("attributes")
    @classmethod
    def validate_attributes(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > _MAX_ATTRIBUTES:
            raise ValueError("too many trace attributes")
        for key, item in value.items():
            if key not in _ATTRIBUTE_KEYS:
                raise ValueError("unsupported trace attribute")
            if isinstance(item, str):
                if key not in _IDENTIFIER_ATTRIBUTE_KEYS or not item.strip() or len(item) > _ATTRIBUTE_STRING_LIMIT:
                    raise ValueError("trace string attribute is blank or too long")
                if key in _IDENTIFIER_ATTRIBUTE_KEYS and not _IDENTIFIER_PATTERN.fullmatch(item):
                    raise ValueError("trace identifier attribute contains unsupported characters")
            elif type(item) is bool:
                if key not in _BOOLEAN_ATTRIBUTE_KEYS:
                    raise ValueError("trace boolean attribute has an invalid key")
            elif type(item) in {int, float}:
                if key not in _NUMERIC_ATTRIBUTE_KEYS:
                    raise ValueError("trace numeric attribute has an invalid key")
                if item < 0:
                    raise ValueError("trace numeric attributes must be non-negative")
                if isinstance(item, float) and (item != item or item in (float("inf"), float("-inf"))):
                    raise ValueError("trace attributes must not contain NaN or Infinity")
            else:
                raise ValueError("trace attributes must be scalar metadata")
        return value

    @model_validator(mode="after")
    def validate_error_state(self) -> "RuntimeTraceSpan":
        has_error = self.error_code is not None or self.error_category is not None
        if self.status == "ok" and has_error:
            raise ValueError("successful spans cannot contain error metadata")
        if self.status != "ok" and (self.error_code is None or self.error_category is None):
            raise ValueError("non-success spans require stable error metadata")
        return self


class RuntimeTracePage(BaseModel):
    """One bounded read from the in-memory Runtime trace ring."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["runtime-trace/v1"]
    items: tuple[RuntimeTraceSpan, ...]
    next_cursor: int = Field(ge=0)
    dropped_total: int = Field(ge=0)
    oldest_sequence: int | None = Field(default=None, ge=1)
    gap_count: int = Field(ge=0)
    runtime_instance_id: UUID
