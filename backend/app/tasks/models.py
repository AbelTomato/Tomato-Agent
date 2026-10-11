from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.runs.models import TaskKind


class UnifiedTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: TaskKind
    message: str = Field(min_length=1)
    client_request_id: str = Field(min_length=1)
    workspace_id: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_kind(self):
        if not self.message.strip():
            raise ValueError("message cannot be blank")
        if (self.kind == "code") != bool(self.workspace_id):
            raise ValueError("workspace is required only for code")
        if self.kind != "knowledge" and self.options:
            raise ValueError("options are supported only for knowledge")
        if set(self.options) - {"retrieval_mode", "limit"}:
            raise ValueError("unknown knowledge options")
        if self.options.get("retrieval_mode", "keyword") not in {"keyword", "vector", "hybrid"}:
            raise ValueError("invalid retrieval_mode")
        limit = self.options.get("limit", 5)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("invalid limit")
        return self


class TaskActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: str = Field(min_length=1)
    action: str
    expected_version: int = Field(ge=0)
    client_request_id: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)