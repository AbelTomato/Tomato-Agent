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


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: UUID
    task_type: str
    request: dict[str, Any]
    workspace_id: str
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
