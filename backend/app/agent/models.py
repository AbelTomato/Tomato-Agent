from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


TaskRunStatus = Literal[
    "queued", "running", "waiting", "completed", "failed", "cancelled", "timed_out"
]


class CodeTaskBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    max_loops: int = Field(default=12, gt=0)
    max_tool_calls: int = Field(default=8, gt=0)
    max_duration_seconds: float = Field(default=120.0, gt=0)
    max_context_tokens: int = Field(default=8000, gt=0)
    max_response_chars: int = Field(default=20000, gt=0)


class CodeTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    task: str = Field(min_length=1)

    @field_validator("task")
    @classmethod
    def require_non_empty_task(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task must contain non-whitespace text")
        return value


class ToolCall(BaseModel):
    call_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    arguments: dict[str, Any]


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class LLMResponse(BaseModel):
    kind: Literal["final", "tool_call", "clarification"]
    content: str | None = None
    tool_call: ToolCall | None = None


class RunResult(BaseModel):
    run_id: UUID
    session_id: UUID
    status: Literal["completed", "paused", "failed"]
    answer: str | None = None
    trace_id: UUID
    loop_count: int = 0
    error: dict[str, Any] | None = None


class ContextState(BaseModel):
    summary: str = ""
    memory: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    active_tasks: list[str] = Field(default_factory=list)
    important_facts: list[str] = Field(default_factory=list)
    compacted_message_count: int = Field(default=0, ge=0)


class Event(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    run_id: UUID
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class SessionRecord(BaseModel):
    id: UUID
    status: str
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class RunRecord(BaseModel):
    id: UUID
    session_id: UUID
    status: str
    loop_count: int
    state: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class CheckpointRecord(BaseModel):
    run_id: UUID
    sequence: int
    state: dict[str, Any]
    created_at: datetime
