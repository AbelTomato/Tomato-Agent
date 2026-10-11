from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.runs.models import TaskKind


class TaskRecoveryObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=0)
    reason: str = Field(min_length=1)
    action: Literal["reuse", "retry", "reconcile", "manual_review", "reject"]
    allowed_actions: list[Literal["cancel", "inspect_recovery", "continue_recovery"]]


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
    model_config = ConfigDict(extra="forbid", strict=True)
    action_id: str = Field(min_length=1)
    action: Literal["cancel", "confirm_outline", "save_draft", "inspect_recovery", "continue_recovery"]
    expected_version: int = Field(ge=0)
    client_request_id: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_payload(self):
        if self.action == "confirm_outline":
            if set(self.payload) != {"outline"} or not isinstance(self.payload["outline"], dict):
                raise ValueError("outline object required")
        elif self.action == "continue_recovery":
            if set(self.payload) != {"confirmed"} or type(self.payload["confirmed"]) is not bool:
                raise ValueError("confirmed boolean required")
        elif self.payload:
            raise ValueError("payload must be empty")
        return self