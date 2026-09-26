from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.knowledge.pipeline_models import EvidenceStatus, RetrievalMode
from app.knowledge.service import CitationSnapshot


class WritingExecutionError(Exception):
    """Stable, safe-to-report error raised by writing execution components."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class OutlineSection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    title: str = Field(min_length=1, max_length=200)
    points: list[str] = Field(min_length=1, max_length=8)
    citation_ids: list[str] = Field(min_length=1)

    @field_validator("title", "points", "citation_ids", mode="before")
    @classmethod
    def strip_text(cls, value):
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return [item.strip() if isinstance(item, str) else item for item in value]
        return value

    @field_validator("points")
    @classmethod
    def validate_points(cls, value: list[str]) -> list[str]:
        if any(not point or len(point) > 1000 for point in value):
            raise ValueError("points must contain nonblank strings up to 1000 characters")
        return value

    @field_validator("citation_ids")
    @classmethod
    def validate_citation_ids(cls, value: list[str]) -> list[str]:
        if any(not item for item in value):
            raise ValueError("citation IDs must not be blank")
        return value


class GeneratedOutline(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    title: str = Field(min_length=1, max_length=200)
    sections: list[OutlineSection] = Field(min_length=1, max_length=12)
    gaps: list[str] = Field(max_length=12)

    @field_validator("title", "gaps", mode="before")
    @classmethod
    def strip_text(cls, value):
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return [item.strip() if isinstance(item, str) else item for item in value]
        return value

    @field_validator("gaps")
    @classmethod
    def validate_gaps(cls, value: list[str]) -> list[str]:
        if any(not gap or len(gap) > 500 for gap in value):
            raise ValueError("gaps must contain nonblank strings up to 500 characters")
        return value


class ExecutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    retrieval_mode: RetrievalMode = "keyword"
    max_evidence: int = Field(default=5, ge=1, le=10)
    max_context_tokens: int = Field(default=8000, gt=0)
    max_response_chars: int = Field(default=20000, gt=0)
    timeout_seconds: float = Field(default=120.0, gt=0)


class ResearchBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_status: EvidenceStatus
    citations: list[CitationSnapshot]
    retrieval_mode: RetrievalMode


class ExecutionAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: UUID
    task_id: UUID
    input_version: int = Field(ge=1)
    run_id: UUID
    status: Literal["running", "completed", "failed", "conflicted"]
    phase: Literal["retrieval", "generation", "validation", "publication"]
    error_code: str | None = None
    config: ExecutionConfig
    model_id: str
    prompt_version: str
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_utc_timestamps(self) -> "ExecutionAttempt":
        if self.created_at.utcoffset() is None or self.updated_at.utcoffset() is None:
            raise ValueError("attempt timestamps must be timezone-aware")
        return self