from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.knowledge.models import SearchResult


RetrievalMode = Literal["keyword", "vector", "hybrid"]
EvidenceStatus = Literal["supported", "insufficient", "no_results"]


class _ImmutableModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RetrievalQuery(_ImmutableModel):
    query_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    facet: str = Field(min_length=1)

    @field_validator("query_id", "text", "facet")
    @classmethod
    def reject_blank_values(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query fields must not be blank")
        return value


class QueryPlan(_ImmutableModel):
    original_query: str = Field(min_length=1)
    queries: tuple[RetrievalQuery, ...] = Field(min_length=1)
    is_multi_evidence: bool

    @field_validator("original_query")
    @classmethod
    def reject_blank_original_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("original_query must not be blank")
        return value

    @model_validator(mode="after")
    def validate_query_ids(self) -> QueryPlan:
        query_ids = [query.query_id for query in self.queries]
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("query_id values must be unique")
        return self


class CandidateEvidence(_ImmutableModel):
    result: SearchResult
    query_ids: tuple[str, ...] = Field(min_length=1)
    retrieval_ranks: tuple[int, ...] = Field(min_length=1)
    retrieval_scores: tuple[float, ...] = ()
    retrieval_score_type: str = Field(min_length=1)
    rerank_score: float | None = None

    @field_validator("query_ids")
    @classmethod
    def validate_query_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not query_id.strip() for query_id in value):
            raise ValueError("query_ids must not contain blank values")
        if len(value) != len(set(value)):
            raise ValueError("query_ids must be unique")
        return value

    @field_validator("retrieval_ranks")
    @classmethod
    def validate_retrieval_ranks(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(rank <= 0 for rank in value):
            raise ValueError("retrieval ranks must be positive")
        return value

    @field_validator("retrieval_scores")
    @classmethod
    def validate_retrieval_scores(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if any(not math.isfinite(score) for score in value):
            raise ValueError("retrieval scores must be finite")
        return value

    @field_validator("retrieval_score_type")
    @classmethod
    def reject_blank_score_type(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("retrieval_score_type must not be blank")
        return value

    @field_validator("rerank_score")
    @classmethod
    def validate_rerank_score(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("rerank_score must be finite")
        return value

    @model_validator(mode="after")
    def validate_result_and_rank_alignment(self) -> CandidateEvidence:
        if not math.isfinite(self.result.score):
            raise ValueError("result score must be finite")
        if len(self.query_ids) != len(self.retrieval_ranks):
            raise ValueError("query_ids and retrieval_ranks must have the same length")
        if self.retrieval_scores and len(self.query_ids) != len(self.retrieval_scores):
            raise ValueError("query_ids and retrieval_scores must have the same length")
        if self.result.end_line < self.result.start_line:
            raise ValueError("result end_line must not precede start_line")
        return self


class SelectionDisposition(_ImmutableModel):
    candidate_index: int = Field(ge=1)
    chunk_id: str = Field(min_length=1)
    selected: bool
    selected_order: int | None = Field(default=None, ge=1)
    excluded_reason: Literal[
        "final_limit", "duplicate_chunk", "not_selected_by_selector"
    ] | None = None

    @model_validator(mode="after")
    def validate_disposition(self) -> SelectionDisposition:
        if self.selected and (self.selected_order is None or self.excluded_reason is not None):
            raise ValueError("selected candidates require an order and no exclusion reason")
        if not self.selected and (self.selected_order is not None or self.excluded_reason is None):
            raise ValueError("excluded candidates require a reason and no selection order")
        return self


class EvidenceSelection(_ImmutableModel):
    selected: tuple[CandidateEvidence, ...]
    covered_query_ids: tuple[str, ...]
    covered_document_ids: tuple[str, ...]
    final_limit: int = Field(gt=0)
    dispositions: tuple[SelectionDisposition, ...] = ()

    @field_validator("covered_query_ids", "covered_document_ids")
    @classmethod
    def validate_covered_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("covered IDs must not contain blank values")
        if len(value) != len(set(value)):
            raise ValueError("covered IDs must be unique")
        return value


class AnswerabilityDecision(_ImmutableModel):
    status: EvidenceStatus
    reason: str = Field(min_length=1)
    coverage_ratio: float = Field(ge=0.0, le=1.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("reason")
    @classmethod
    def reject_blank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value

    @field_validator("coverage_ratio", "confidence")
    @classmethod
    def validate_finite_scores(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("decision scores must be finite")
        return value


class PipelineResult(_ImmutableModel):
    plan: QueryPlan
    candidates: tuple[CandidateEvidence, ...]
    selection: EvidenceSelection
    decision: AnswerabilityDecision
    query_planner_latency_ms: float = Field(default=0.0, ge=0.0)
    candidate_latency_ms: float = Field(ge=0.0)
    rerank_latency_ms: float = Field(ge=0.0)
    selection_latency_ms: float = Field(ge=0.0)
    judge_latency_ms: float = Field(ge=0.0)

    @field_validator(
        "query_planner_latency_ms",
        "candidate_latency_ms",
        "rerank_latency_ms",
        "selection_latency_ms",
        "judge_latency_ms",
    )
    @classmethod
    def validate_finite_latencies(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("latencies must be finite")
        return value