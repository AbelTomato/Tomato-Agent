from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.artifacts.models import ArtifactRef
from app.knowledge.service import CitationSnapshot
from app.runs.models import PendingAction


ValidationStatus = Literal["not_required", "passed", "failed", "unavailable"]
FailureCategory = Literal[
    "test_failed", "recoverable_error", "permission_denied", "user_rejected",
    "unavailable", "ownership_lost", "unknown_side_effect", "internal_error",
]


class AssistantModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class MessageDecision(AssistantModel):
    protocol_version: Literal[1] = 1
    type: Literal["message"]
    payload: "MessagePayload"


class MessagePayload(AssistantModel):
    text: str = Field(min_length=1)


class AskUserDecision(AssistantModel):
    protocol_version: Literal[1] = 1
    type: Literal["ask_user"]
    payload: "AskUserPayload"


class AskUserPayload(AssistantModel):
    question: str = Field(min_length=1)
    choices: list[str] = Field(default_factory=list)
    allow_free_text: Literal[True] = True


class DiscoverToolsDecision(AssistantModel):
    protocol_version: Literal[1] = 1
    type: Literal["discover_tools"]
    payload: "DiscoverToolsPayload"


class DiscoverToolsPayload(AssistantModel):
    category: str | None = None
    query: str | None = None


class DescribeToolDecision(AssistantModel):
    protocol_version: Literal[1] = 1
    type: Literal["describe_tool"]
    payload: "DescribeToolPayload"


class DescribeToolPayload(AssistantModel):
    name: str = Field(min_length=1)


class CallToolDecision(AssistantModel):
    protocol_version: Literal[1] = 1
    type: Literal["call_tool"]
    payload: "CallToolPayload"


class CallToolPayload(AssistantModel):
    name: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    arguments: dict[str, Any]


class FinishDecision(AssistantModel):
    protocol_version: Literal[1] = 1
    type: Literal["finish"]
    payload: "FinishPayload"


class FinishPayload(AssistantModel):
    result: AssistantResult


AssistantDecision = Annotated[
    Union[
        MessageDecision,
        AskUserDecision,
        DiscoverToolsDecision,
        DescribeToolDecision,
        CallToolDecision,
        FinishDecision,
    ],
    Field(discriminator="type"),
]


class RuntimeEventDetail(AssistantModel):
    data: dict[str, JsonValue] = Field(default_factory=dict)


class RuntimeEvent(AssistantModel):
    schema_version: Literal[1] = 1
    event_id: UUID
    run_id: UUID
    step_id: str | None = Field(default=None, min_length=1)
    invocation_id: str | None = Field(default=None, min_length=1)
    event_type: str = Field(min_length=1)
    occurred_at: datetime
    summary: str = Field(min_length=1)
    detail: RuntimeEventDetail


class AssistantResult(AssistantModel):
    schema_version: Literal[1] = 1
    answer: str = ""
    citations: list[CitationSnapshot] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    test_results: list[dict[str, Any]] = Field(default_factory=list)
    validation_status: ValidationStatus = "not_required"


class CapabilityError(AssistantModel):
    category: FailureCategory
    code: str = Field(min_length=1)


class CapabilityDescriptor(AssistantModel):
    """Server-owned metadata; neither tool output nor user approval grants it."""

    name: str = Field(min_length=1)
    parameters: dict[str, Any]
    requires_workspace: bool
    side_effect: Literal["read_only", "file_write", "artifact", "sandbox", "domain_publication"]
    recovery_class: Literal["read_only", "file_write", "artifact", "sandbox", "domain_publication"]
    available: bool
    reason: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    completion_requirements: list[str] = Field(default_factory=list)


class CapabilityOutcome(AssistantModel):
    schema_version: Literal[1] = 1
    kind: Literal["result", "waiting", "failed"]
    result: AssistantResult | None = None
    pending_action: PendingAction | None = None
    error: CapabilityError | None = None

    @model_validator(mode="after")
    def validate_outcome(self):
        if self.kind == "result":
            if self.result is None or self.pending_action is not None or self.error is not None:
                raise ValueError("result outcome requires only a result")
        elif self.kind == "waiting":
            if self.pending_action is None:
                raise ValueError("waiting outcome requires a pending action")
            if self.error is not None and self.error.category != "unknown_side_effect":
                raise ValueError("only unknown side effects may accompany waiting")
        elif self.error is None or self.pending_action is not None:
            raise ValueError("failed outcome requires an error and no pending action")
        return self