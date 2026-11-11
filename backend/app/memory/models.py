from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


MemoryKind = Literal["preference", "habit", "background", "episodic"]
MemoryScope = Literal["personal", "project", "session"]


class MemorySourceInput(BaseModel):
    source_system: str = Field(min_length=1, max_length=80)
    source_installation_id: str = Field(min_length=1, max_length=160)
    session_id: str = Field(min_length=1, max_length=160)
    message_id: str = Field(min_length=1, max_length=160)
    source_revision: int = Field(default=1, ge=1)
    role: Literal["user"] = "user"
    message_at: str | None = Field(default=None, max_length=80)
    excerpt: str | None = Field(default=None, max_length=1000)
    locator: str | None = Field(default=None, max_length=500)


class MemoryItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: MemoryKind
    scope: MemoryScope = "personal"
    scope_id: str = Field(min_length=1, max_length=160)
    topic_key: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=4000)
    confirmation: Literal["explicit", "automatic", "accepted"] = "explicit"
    sources: list[MemorySourceInput] = Field(default_factory=list, max_length=20)


class MemoryItemPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    content: str = Field(min_length=1, max_length=4000)
    reason: str = Field(default="user_correction", min_length=1, max_length=500)
    expected_version: int = Field(ge=1)


class MemoryItemView(BaseModel):
    item_id: str
    kind: MemoryKind
    scope: MemoryScope
    scope_id: str
    topic_key: str
    status: Literal["active", "superseded", "forgotten"]
    confirmation: Literal["explicit", "automatic", "accepted"]
    version: int
    content: str
    created_at: str
    updated_at: str


class MemoryTurnMessage(BaseModel):
    message_id: str = Field(min_length=1, max_length=160)
    role: Literal["user", "assistant"]
    content: str = Field(max_length=6000)
    message_at: str | None = Field(default=None, max_length=80)
    revision: int = Field(default=1, ge=1)


class MemoryTurnIngest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_system: str = Field(min_length=1, max_length=80)
    source_installation_id: str = Field(min_length=1, max_length=160)
    session_id: str = Field(min_length=1, max_length=160)
    messages: list[MemoryTurnMessage] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def bound_extraction_input(self):
        if sum(len(message.content) for message in self.messages) > 6000:
            raise ValueError("memory_extraction_input_too_large")
        return self


class MemoryExplicitIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_system: str = Field(min_length=1, max_length=80)
    source_installation_id: str = Field(min_length=1, max_length=160)
    session_id: str = Field(min_length=1, max_length=160)
    message: MemoryTurnMessage


class MemoryRecallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(min_length=1, max_length=3000)
    project_scope_id: str | None = Field(default=None, max_length=160)
    session_scope_id: str | None = Field(default=None, max_length=160)
    limit: int = Field(default=8, ge=1, le=8)
    budget_chars: int = Field(default=1200, ge=100, le=1200)


class MemoryCandidateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["accept", "reject"]
