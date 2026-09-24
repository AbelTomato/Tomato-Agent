"""Contracts for reproducible retrieval benchmark manifests and reports."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _ImmutableModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


RetrievalMode = Literal["keyword", "vector", "hybrid"]
ComparisonScope = Literal["fixed_index", "index_strategy"]
QuestionStatus = Literal["complete", "error", "skipped"]
RunStatus = Literal["complete", "incomplete", "failed"]
ComparisonStatus = Literal["improved", "unchanged", "mixed", "regressed", "incomparable", "incomplete"]


class StrategyConfig(_ImmutableModel):
    name: str = Field(min_length=1)
    mode: RetrievalMode
    top_k: int = Field(ge=1)
    candidate_limit: int = Field(ge=1)
    final_limit: int = Field(ge=1)
    similarity_threshold: float = Field(ge=0.0, le=1.0)
    parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def reject_blank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("strategy name must not be blank")
        return value

    @field_validator("similarity_threshold")
    @classmethod
    def require_finite_threshold(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("similarity_threshold must be finite")
        return value

    @field_validator("parameters")
    @classmethod
    def validate_parameters(
        cls, values: dict[str, str | int | float | bool]
    ) -> dict[str, str | int | float | bool]:
        allowed = {
            "embedding_model",
            "embedding_dimensions",
            "chunk_size",
            "chunk_overlap",
            "score_type",
            "query_cache",
        }
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ValueError(f"unknown strategy parameters: {', '.join(unknown)}")
        return values


class EvidenceSpan(_ImmutableModel):
    document_id: str = Field(min_length=1)
    document_version: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_line_range(self) -> EvidenceSpan:
        if self.end_line < self.start_line:
            raise ValueError("evidence end_line must not precede start_line")
        return self


class QuestionResult(_ImmutableModel):
    question_id: str = Field(min_length=1)
    group_id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    split: str = Field(min_length=1)
    answerable: bool
    relevant_spans: tuple[EvidenceSpan, ...] = ()
    retrieved_chunk_ids: tuple[str, ...] = ()
    retrieved_ranks: tuple[int, ...] = ()
    retrieved_scores: tuple[float, ...] = ()
    score_type: str | None = None
    hit_relevant_spans: int = Field(ge=0)
    recall_at_1: float | None = Field(default=None, ge=0.0, le=1.0)
    recall_at_3: float | None = Field(default=None, ge=0.0, le=1.0)
    recall_at_5: float | None = Field(default=None, ge=0.0, le=1.0)
    recall: float | None = Field(default=None, ge=0.0, le=1.0)
    reciprocal_rank: float | None = Field(default=None, ge=0.0, le=1.0)
    hit: bool | None = None
    status: QuestionStatus
    error_code: str | None = None
    duration_ms: float | None = Field(default=None, ge=0.0)

    @field_validator("question_id", "group_id", "category", "split")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question fields must not be blank")
        return value

    @field_validator("retrieved_chunk_ids")
    @classmethod
    def validate_chunk_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("retrieved chunk IDs must not be blank")
        if len(set(values)) != len(values):
            raise ValueError("retrieved chunk IDs must be unique")
        return values

    @field_validator("retrieved_ranks")
    @classmethod
    def validate_retrieved_ranks(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if any(value < 1 for value in values):
            raise ValueError("retrieved ranks must be positive")
        if tuple(sorted(values)) != values:
            raise ValueError("retrieved ranks must be ordered")
        return values

    @field_validator("retrieved_scores")
    @classmethod
    def validate_retrieved_scores(cls, values: tuple[float, ...]) -> tuple[float, ...]:
        if any(not math.isfinite(value) for value in values):
            raise ValueError("retrieved scores must be finite")
        return values

    @field_validator(
        "recall_at_1", "recall_at_3", "recall_at_5", "recall", "reciprocal_rank", "duration_ms"
    )
    @classmethod
    def require_finite_numbers(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("numeric result fields must be finite")
        return value

    @model_validator(mode="after")
    def validate_question_result(self) -> QuestionResult:
        if not self.answerable and self.relevant_spans:
            raise ValueError("unanswerable questions must have empty relevant_spans")
        if self.hit_relevant_spans > len(self.relevant_spans):
            raise ValueError("hit_relevant_spans exceeds relevant span count")
        if self.status == "error" and not self.error_code:
            raise ValueError("error questions require error_code")
        if self.status != "error" and self.error_code is not None:
            raise ValueError("error_code is only valid for error questions")
        return self


class BenchmarkManifest(_ImmutableModel):
    schema_version: str = Field(min_length=1)
    dataset: str = Field(min_length=1)
    split: str = Field(min_length=1)
    database: str = Field(min_length=1)
    comparison_scope: ComparisonScope
    allowed_changes: tuple[str, ...] = ()
    strategy: StrategyConfig
    question_ids: tuple[str, ...] = ()
    question_groups: dict[str, str] = Field(default_factory=dict)
    question_splits: dict[str, str] = Field(default_factory=dict)
    dataset_sha256: str | None = None
    index_sha256: str | None = None
    source_sha256: str | None = None
    config_sha256: str | None = None

    @field_validator("schema_version", "dataset", "split", "database")
    @classmethod
    def reject_blank_manifest_values(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("manifest values must not be blank")
        return value

    @field_validator("allowed_changes")
    @classmethod
    def reject_blank_changes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("allowed changes must not be blank")
        if len(set(values)) != len(values):
            raise ValueError("allowed changes must be unique")
        return values

    @field_validator("question_ids")
    @classmethod
    def validate_question_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("question IDs must not be blank")
        if len(set(values)) != len(values):
            raise ValueError("Duplicate question_id found")
        return values

    @model_validator(mode="after")
    def validate_question_groups_and_splits(self) -> BenchmarkManifest:
        if not self.question_ids:
            return self
        question_ids = set(self.question_ids)
        if set(self.question_groups) != question_ids:
            raise ValueError("question_groups must cover every question_id")
        if set(self.question_splits) != question_ids:
            raise ValueError("question_splits must cover every question_id")
        groups_by_split: dict[str, set[str]] = {}
        for question_id in self.question_ids:
            question_split = self.question_splits[question_id]
            if not question_split.strip():
                raise ValueError("question split must not be blank")
            groups_by_split.setdefault(self.question_groups[question_id], set()).add(
                question_split
            )
        leaked_groups = [group for group, splits in groups_by_split.items() if len(splits) > 1]
        if leaked_groups:
            raise ValueError(f"group_id leakage across splits: {', '.join(leaked_groups)}")
        return self


class RunReport(_ImmutableModel):
    schema_version: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    manifest: BenchmarkManifest
    strategy: StrategyConfig
    dataset_sha256: str = Field(min_length=1)
    corpus_sha256: str = Field(min_length=1)
    index_sha256: str = Field(min_length=1)
    source_sha256: str = Field(min_length=1)
    metric_version: str = Field(min_length=1)
    status: RunStatus
    questions: tuple[QuestionResult, ...]
    expected_question_ids: tuple[str, ...]
    summary: dict[str, Any]

    @model_validator(mode="after")
    def validate_question_coverage(self) -> RunReport:
        expected = set(self.expected_question_ids)
        actual = [question.question_id for question in self.questions]
        if len(actual) != len(set(actual)):
            raise ValueError("run questions must have unique question IDs")
        if set(actual) != expected:
            raise ValueError("run question results must cover expected question IDs")
        if self.status == "complete" and any(question.status != "complete" for question in self.questions):
            raise ValueError("complete run cannot contain incomplete question results")
        return self


class MetricValue(_ImmutableModel):
    value: float | None = None
    sample_count: int = Field(ge=0)

    @field_validator("value")
    @classmethod
    def require_finite_metric(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("metric values must be finite")
        return value


class EvaluationSummary(_ImmutableModel):
    question_count: int = Field(ge=0)
    answerable_count: int = Field(ge=0)
    unanswerable_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    execution_error_rate: MetricValue
    answerable_empty_result_rate: MetricValue
    unanswerable_empty_result_rate: MetricValue
    unanswerable_nonempty_rate: MetricValue
    recall_at_1: MetricValue
    recall_at_3: MetricValue
    recall_at_5: MetricValue
    mrr_at_5: MetricValue
    hit_at_5: MetricValue
    by_category: dict[str, dict[str, MetricValue]]


class MetricDelta(_ImmutableModel):
    baseline: float | None = None
    candidate: float | None = None
    absolute: float | None = None
    relative: float | None = None

    @field_validator("baseline", "candidate", "absolute", "relative")
    @classmethod
    def require_finite_delta(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("metric deltas must be finite")
        return value


class QuestionDelta(_ImmutableModel):
    question_id: str = Field(min_length=1)
    baseline_status: str = Field(min_length=1)
    candidate_status: str = Field(min_length=1)
    classification: Literal["improved", "unchanged", "regressed", "error"]


class ComparisonReport(_ImmutableModel):
    schema_version: str = Field(min_length=1)
    baseline_run_id: str = Field(min_length=1)
    candidate_run_id: str = Field(min_length=1)
    status: ComparisonStatus
    reason: str = Field(min_length=1)
    metric_deltas: dict[str, MetricDelta]
    question_deltas: tuple[QuestionDelta, ...]

    @model_validator(mode="after")
    def validate_status_reason(self) -> ComparisonReport:
        if self.status == "incomparable" and not self.reason.strip():
            raise ValueError("incomparable comparisons require a reason")
        return self
