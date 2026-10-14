from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.runs.models import TaskAction, TaskKind
from app.writing.execution_models import GeneratedOutline


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
        if self.kind == "assistant":
            if not self.client_request_id.strip():
                raise ValueError("client_request_id cannot be blank")
            if self.workspace_id is not None and not self.workspace_id.strip():
                raise ValueError("workspace_id cannot be blank")
            if set(self.options) - {"allow_test_execution"}:
                raise ValueError("unknown assistant options")
            if "allow_test_execution" in self.options and type(self.options["allow_test_execution"]) is not bool:
                raise ValueError("allow_test_execution must be boolean")
            return self
        elif (self.kind == "code") != bool(self.workspace_id):
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
    action: TaskAction
    expected_version: int = Field(ge=0)
    client_request_id: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_payload(self):
        if self.action in {"provide_input", "approve_action", "reject_action", "bind_workspace"}:
            if not self.action_id.strip() or not self.client_request_id.strip():
                raise ValueError("action identity cannot be blank")
        if self.action == "confirm_outline":
            if set(self.payload) != {"outline"} or not isinstance(self.payload["outline"], dict):
                raise ValueError("outline object required")
        elif self.action == "continue_recovery":
            if set(self.payload) != {"confirmed"} or type(self.payload["confirmed"]) is not bool:
                raise ValueError("confirmed boolean required")
        elif self.action == "resolve_side_effect":
            if (not {"resolution"} <= set(self.payload) or set(self.payload) - {"resolution", "accept_risk"}
                    or not isinstance(self.payload["resolution"], str)
                    or self.payload["resolution"] not in {"executed", "not_executed", "unknown"}):
                raise ValueError("side effect resolution required")
            if "accept_risk" in self.payload and type(self.payload["accept_risk"]) is not bool:
                raise ValueError("accept_risk must be boolean")
        elif self.action == "provide_input":
            if (len(self.payload) != 1 or not set(self.payload) <= {"message", "choice"}
                    or not isinstance(next(iter(self.payload.values())), str)
                    or not next(iter(self.payload.values())).strip()):
                raise ValueError("nonblank answer required")
        elif self.action == "bind_workspace":
            key = "workspace_id"
            if (set(self.payload) != {key} or not isinstance(self.payload[key], str)
                    or not self.payload[key].strip()):
                raise ValueError(f"nonblank {key} required")
        elif self.action == "approve_action" and self.payload:
            if set(self.payload) != {"outline"} or not isinstance(self.payload["outline"], dict):
                raise ValueError("only outline object allowed")
            # Structural validation here; evidence membership is checked against
            # the server's frozen citation snapshot when the action is executed.
            GeneratedOutline.model_validate(self.payload["outline"], strict=True)
        elif self.payload:
            raise ValueError("payload must be empty")
        return self


class SteerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    client_request_id: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=20000)

    @model_validator(mode="after")
    def nonblank(self):
        if not self.message.strip() or not self.client_request_id.strip():
            raise ValueError("nonblank steer required")
        return self