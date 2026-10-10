from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.models import LLMResponse, Message

PROTOCOL_VERSION = 1

AttemptStatus = Literal["running", "succeeded", "failed", "cancelled", "expired", "released"]
OperationStatus = Literal["prepared", "running", "succeeded", "failed", "unknown"]
StepStatus = Literal["running", "succeeded", "failed", "unknown"]
SnapshotPhase = Literal["ready_model", "pending_tool", "ready_finish", "waiting"]


class ExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def _require_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return value
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include timezone information")
    return value


class LeaseHandle(ExecutionModel):
    run_id: UUID
    attempt_id: UUID
    owner_id: str = Field(min_length=1)
    fencing_token: int = Field(ge=0)
    lease_expires_at: datetime

    _validate_expiry = field_validator("lease_expires_at")(_require_aware)


class AttemptRecord(ExecutionModel):
    id: UUID
    run_id: UUID
    attempt_no: int = Field(gt=0)
    owner_id: str = Field(min_length=1)
    fencing_token: int = Field(ge=0)
    status: AttemptStatus
    lease_expires_at: datetime
    heartbeat_at: datetime
    started_at: datetime
    finished_at: datetime | None = None
    error_code: str | None = None

    _validate_times = field_validator(
        "lease_expires_at", "heartbeat_at", "started_at", "finished_at"
    )(_require_aware)


class BudgetState(ExecutionModel):
    max_model_calls: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    reserved_model_calls: int = Field(default=0, ge=0)
    reserved_tool_calls: int = Field(default=0, ge=0)

    def reserve_model_call(self) -> "BudgetState":
        if self.reserved_model_calls >= self.max_model_calls:
            raise ValueError("model call budget exhausted")
        return self.model_copy(update={"reserved_model_calls": self.reserved_model_calls + 1})

    def reserve_tool_call(self) -> "BudgetState":
        if self.reserved_tool_calls >= self.max_tool_calls:
            raise ValueError("tool call budget exhausted")
        return self.model_copy(update={"reserved_tool_calls": self.reserved_tool_calls + 1})

    def record_failed_model_call(self) -> "BudgetState":
        return self.model_copy(deep=True)

    def record_failed_tool_call(self) -> "BudgetState":
        return self.model_copy(deep=True)


class ExecutionSnapshot(ExecutionModel):
    run_id: UUID
    protocol_version: int = Field(default=PROTOCOL_VERSION, ge=1)
    schema_version: int = Field(ge=1)
    checkpoint_revision: int = Field(ge=1)
    last_event_sequence: int = Field(ge=0)
    phase: SnapshotPhase
    logical_index: int = Field(ge=0)
    messages: list[Message]
    harness_state: dict[str, Any]
    pending_operation_id: UUID | None = None
    pending_response: LLMResponse | None = None
    budget_limits: dict[str, int | float]
    reserved_model_calls: int = Field(ge=0)
    reserved_tool_calls: int = Field(ge=0)
    loop_count: int = Field(ge=0)
    elapsed_upper_bound_seconds: float = Field(ge=0)
    policy_digest: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    test_results: list[dict[str, Any]]
    artifact_ids: list[str]
    pending_decision: dict[str, Any] | None = None
    in_flight_started_at: datetime | None = None
    in_flight_max_seconds: float = Field(default=0.0, ge=0)

    _validate_in_flight = field_validator("in_flight_started_at")(_require_aware)

    @field_validator("protocol_version")
    @classmethod
    def require_current_protocol(cls, value: int) -> int:
        if value != PROTOCOL_VERSION:
            raise ValueError(f"unsupported execution protocol version: {value}")
        return value

    @field_validator("budget_limits")
    @classmethod
    def require_budget_limits(cls, value: dict[str, int | float]) -> dict[str, int | float]:
        if not value:
            raise ValueError("budget_limits must not be empty")
        return deepcopy(value)


class OperationInput(ExecutionModel):
    id: UUID
    logical_index: int = Field(ge=0)
    kind: Literal["model", "tool"]
    tool_name: str | None = None
    input_payload: dict[str, Any]
    recovery_class: Literal["model", "read_only", "file_write", "artifact", "sandbox"]
    before_digests: dict[str, str] = Field(default_factory=dict)
    after_digests: dict[str, str] = Field(default_factory=dict)

    @field_validator("input_payload")
    @classmethod
    def reject_sensitive_input(cls, value: dict[str, Any]) -> dict[str, Any]:
        sensitive = {"authorization", "api_key", "apikey", "credential", "password", "secret", "token"}
        def contains(item, key=None):
            if key and key.lower().replace("-", "_") in sensitive:
                return True
            if isinstance(item, dict):
                return any(contains(v, str(k)) for k, v in item.items())
            if isinstance(item, (list, tuple, set, frozenset)):
                return any(contains(v) for v in item)
            return False
        if contains(value):
            raise ValueError("operation input must not contain sensitive fields")
        return value


class StepRecord(ExecutionModel):
    id: UUID
    operation_id: UUID
    attempt_id: UUID
    execution_no: int = Field(gt=0)
    status: StepStatus


class StepOutcome(ExecutionModel):
    success: bool
    result_payload: dict[str, Any]
