from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


RunStatus = Literal[
    "queued",
    "running",
    "waiting",
    "completed",
    "failed",
    "cancelled",
    "timed_out",
]

TaskKind = Literal["chat", "knowledge", "writing", "code", "assistant"]
TaskAction = Literal[
    "cancel", "confirm_outline", "save_draft", "inspect_recovery", "continue_recovery",
    "provide_input", "approve_action", "reject_action", "bind_workspace",
    "resolve_side_effect",
]


class PendingAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    allowed_actions: list[TaskAction] = Field(min_length=1)
    business_version: int = Field(ge=0)
    display_data: dict[str, Any] = Field(default_factory=dict)
    action_type: Literal["legacy", "clarification", "confirmation", "workspace_binding", "recovery"] = "legacy"
    target_capability: str | None = Field(default=None, min_length=1)
    frozen_parameters: dict[str, Any] = Field(default_factory=dict)


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: UUID
    task_type: str
    request: dict[str, Any]
    workspace_id: str | None = None
    session_id: UUID | None = None
    client_request_id: str | None = None
    domain_run_id: str | None = None
    result: dict[str, Any] | None = None
    stage: str | None = None
    pending_action: PendingAction | None = None
    status: RunStatus
    error: dict[str, Any] | None = None
    version: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class RunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: UUID
    run_id: UUID
    sequence: int = Field(gt=0)
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class RunCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: UUID
    sequence: int = Field(gt=0)
    state: dict[str, Any]
    created_at: datetime


class RunRepositoryConflict(RuntimeError):
    """Raised when an optimistic update used a stale run version."""
