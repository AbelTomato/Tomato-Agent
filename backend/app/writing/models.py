from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.knowledge.service import CitationSnapshot


class WritingStatus(StrEnum):
    RESEARCHING = "researching"
    AWAITING_OUTLINE_CONFIRMATION = "awaiting_outline_confirmation"
    DRAFTING = "drafting"
    AWAITING_SAVE_CONFIRMATION = "awaiting_save_confirmation"
    SAVED = "saved"
    FAILED = "failed"


class WritingTask(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: UUID
    session_id: UUID
    topic: str = Field(min_length=1)
    status: WritingStatus
    version: int = Field(ge=1)
    outline: dict[str, Any] = Field(default_factory=dict)
    draft: str | None = None
    citations: list[CitationSnapshot] = Field(default_factory=list)
    research_run_id: UUID | None = None
    drafting_run_id: UUID | None = None
    saved_path: str | None = None
    failed_stage: str | None = None
    created_at: datetime
    updated_at: datetime
