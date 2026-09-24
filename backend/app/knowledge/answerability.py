from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from app.knowledge.pipeline_models import (
    AnswerabilityDecision,
    EvidenceSelection,
    QueryPlan,
)


class AnswerabilityConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    min_supported_coverage: float = Field(default=1.0, ge=0.0, le=1.0)
    min_partial_coverage: float = Field(default=0.5, ge=0.0, le=1.0)
    min_supported_evidence: int = Field(default=1, gt=0)
    multi_evidence_requires_all_queries: StrictBool = True
    allow_insufficient_llm: StrictBool = False
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_coverage_thresholds(self) -> AnswerabilityConfig:
        if self.min_partial_coverage > self.min_supported_coverage:
            raise ValueError("partial coverage threshold must not exceed supported threshold")
        return self


class AnswerabilityJudge(Protocol):
    def judge(
        self,
        plan: QueryPlan,
        selection: EvidenceSelection,
        *,
        config: AnswerabilityConfig,
    ) -> AnswerabilityDecision:
        ...


class BaselineAnswerabilityJudge:
    """Preserve retrieval-only baseline semantics behind an explicit judge."""

    def judge(
        self,
        plan: QueryPlan,
        selection: EvidenceSelection,
        *,
        config: AnswerabilityConfig,
    ) -> AnswerabilityDecision:
        if not selection.selected:
            return AnswerabilityDecision(
                status="no_results",
                reason="no_candidates",
                coverage_ratio=0.0,
                confidence=None,
            )
        required_query_ids = {query.query_id for query in plan.queries}
        covered_query_ids = required_query_ids.intersection(selection.covered_query_ids)
        coverage_ratio = len(covered_query_ids) / len(required_query_ids)
        return AnswerabilityDecision(
            status="supported",
            reason="non_empty_selection",
            coverage_ratio=coverage_ratio,
            confidence=None,
        )


class CoverageAnswerabilityJudge:
    def judge(
        self,
        plan: QueryPlan,
        selection: EvidenceSelection,
        *,
        config: AnswerabilityConfig,
    ) -> AnswerabilityDecision:
        selected_count = len(selection.selected)
        if selected_count == 0:
            return AnswerabilityDecision(
                status="no_results",
                reason="no_candidates",
                coverage_ratio=0.0,
                confidence=None,
            )

        required_query_ids = {query.query_id for query in plan.queries}
        covered_query_ids = required_query_ids.intersection(selection.covered_query_ids)
        coverage_ratio = len(covered_query_ids) / len(required_query_ids)
        confidence = self._confidence(selection)

        if len(selection.selected) < config.min_supported_evidence:
            return self._insufficient("insufficient_evidence", coverage_ratio, confidence)
        if not covered_query_ids:
            return self._insufficient("missing_required_query", coverage_ratio, confidence)
        if config.multi_evidence_requires_all_queries and plan.is_multi_evidence:
            fully_covered = covered_query_ids == required_query_ids
        else:
            fully_covered = coverage_ratio >= config.min_supported_coverage
        if not fully_covered:
            reason = (
                "partial_coverage"
                if coverage_ratio >= config.min_partial_coverage
                else "missing_required_query"
            )
            return self._insufficient(reason, coverage_ratio, confidence)
        if config.min_confidence is not None and (
            confidence is None or confidence < config.min_confidence
        ):
            return self._insufficient("low_rerank_confidence", coverage_ratio, confidence)
        return AnswerabilityDecision(
            status="supported",
            reason="full_query_coverage",
            coverage_ratio=coverage_ratio,
            confidence=confidence,
        )

    @staticmethod
    def _confidence(selection: EvidenceSelection) -> float | None:
        scores = [
            candidate.rerank_score
            for candidate in selection.selected
            if candidate.rerank_score is not None
        ]
        return min(scores) if scores else None

    @staticmethod
    def _insufficient(
        reason: str,
        coverage_ratio: float,
        confidence: float | None,
    ) -> AnswerabilityDecision:
        return AnswerabilityDecision(
            status="insufficient",
            reason=reason,
            coverage_ratio=coverage_ratio,
            confidence=confidence,
        )