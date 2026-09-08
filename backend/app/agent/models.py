from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


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
