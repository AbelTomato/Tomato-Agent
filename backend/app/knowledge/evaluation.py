"""Offline retrieval evaluation and reproducible command-line reporting."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable, Sequence

from app.knowledge.embeddings import EmbeddingClient
from app.knowledge.answerability import (
    AnswerabilityConfig,
    BaselineAnswerabilityJudge,
    CoverageAnswerabilityJudge,
)
from app.knowledge.candidate_retrieval import RepositoryCandidateRetriever
from app.knowledge.evidence_selection import (
    BaselineEvidenceSelector,
    CoverageAwareEvidenceSelector,
)
from app.knowledge.models import SearchResult
from app.knowledge.pipeline_models import CandidateEvidence, PipelineResult
from app.knowledge.query_planning import (
    LLMQueryPlanner,
    QueryPlannerConfig,
    SafeQueryPlanner,
)
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.reranking import NoopReranker
from app.knowledge.service import KnowledgePipeline
from app.settings import settings

RelevantSpan = dict[str, object]
SearchFunction = Callable[[str, int], Awaitable[list[SearchResult]]]
EvidenceItem = SearchResult | CandidateEvidence


class OfflineFakeReranker:
    """Deterministic local reranker for offline ablation tests.

    This is deliberately not presented as a provider result. It only gives the
    fixed ablation a reproducible rerank branch without making network calls.
    """

    async def rank(
        self,
        query: str,
        candidates: Sequence[CandidateEvidence],
    ) -> list[CandidateEvidence]:
        query_terms = set(_evaluation_terms(query))
        ranked: list[tuple[int, float, CandidateEvidence]] = []
        for index, candidate in enumerate(candidates):
            text_terms = set(_evaluation_terms(candidate.result.text))
            score = (
                len(query_terms.intersection(text_terms)) / len(query_terms)
                if query_terms
                else 0.0
            )
            ranked.append(
                (
                    index,
                    score,
                    candidate.model_copy(update={"rerank_score": score}),
                )
            )
        ranked.sort(key=lambda item: (-item[1], item[0]))
        return [item[2] for item in ranked]


def _evaluation_terms(value: str) -> list[str]:
    terms: list[str] = []
    for match in re.finditer(r"[A-Za-z_][A-Za-z0-9_.-]*|[\u4e00-\u9fff]+", value.casefold()):
        token = match.group(0)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            terms.extend(token[index : index + 2] for index in range(len(token) - 1))
        else:
            terms.append(token)
    return terms


@dataclass(frozen=True)
class EvaluationQuestion:
    question_id: str
    query: str
    category: str
    split: str
    answerable: bool
    relevant_spans: tuple[RelevantSpan, ...]
    reference_answer: str


@dataclass(frozen=True)
class EvaluationSummary:
    question_count: int
    answerable_count: int
    recall_at_k: float | None
    mrr_at_k: float | None
    unanswerable_count: int
    correct_refusal_count: int
    incorrect_answer_count: int
    unanswerable_nonempty_count: int = 0
    incorrect_refusal_count: int = 0
    refusal_evaluation: str = "retrieval_only"


@dataclass(frozen=True)
class QuestionResult:
    question_id: str
    category: str
    answerable: bool
    status: str
    score_type: str
    latency_ms: float | None
    recall_at_k: float | None
    mrr_at_k: float | None
    hit_at_k: bool | None
    error: str | None
    retrieved_results: tuple[dict[str, object], ...]
    candidate_results: tuple[dict[str, object], ...] = ()
    final_results: tuple[dict[str, object], ...] = ()
    candidate_evidence_recall_at_k: float | None = None
    candidate_mrr_at_k: float | None = None
    candidate_hit_at_k: bool | None = None
    final_evidence_recall: float | None = None
    source_coverage: dict[str, int] | None = None
    evidence_status: str | None = None
    judge_reason: str | None = None
    judge_executed: bool | None = None
    phase_latency_ms: dict[str, float | None] | None = None
    llm_called: bool | None = None
    citation_complete: bool | None = None
    query_plan: dict[str, object] | None = None
    candidate_nonempty: bool = False
    final_nonempty: bool = False
    judge_status: str | None = None
    incorrect_refusal: bool | None = None
    correct_refusal: bool | None = None


@dataclass(frozen=True)
class QuestionResultSummary:
    question_count: int
    answerable_count: int
    evaluated_answerable_count: int
    recall_at_k: float | None
    mrr_at_k: float | None
    hit_at_k: float | None
    answerable_empty_result_count: int
    unanswerable_count: int
    evaluated_unanswerable_count: int
    unanswerable_empty_result_count: int
    correct_refusal_count: int
    incorrect_answer_count: int
    execution_error_count: int
    retrieval_latency_ms: dict[str, float | int | None]
    candidate_evidence_recall_at_k: float | None = None
    candidate_mrr_at_k: float | None = None
    candidate_hit_at_k: float | None = None
    final_evidence_recall: float | None = None
    source_coverage: dict[str, int] | None = None
    unanswerable_nonempty_result_count: int = 0
    unanswerable_candidate_nonempty_result_count: int = 0
    phase_latency_ms: dict[str, dict[str, float | int | None]] | None = None
    candidate_nonempty_count: int = 0
    final_nonempty_count: int = 0
    judge_executed_count: int = 0
    incorrect_refusal_count: int = 0
    judge_status_counts: dict[str, int] | None = None
    judge_reason_counts: dict[str, int] | None = None


QUESTION_STATUS_SUCCESS = "success"
QUESTION_STATUS_EMPTY = "empty"
QUESTION_STATUS_ERROR = "error"

_ABLATION_FIELDS = {
    "name",
    "split",
    "mode",
    "candidate_limit",
    "candidate_min_vector_similarity",
    "final_limit",
    "query_planning",
    "rerank",
    "evidence_selection",
    "answerability",
}


class _EvaluationArgumentParser(argparse.ArgumentParser):
    def parse_args(self, args: Sequence[str] | None = None, namespace=None):
        parsed = super().parse_args(args, namespace)
        if parsed.final_limit is None:
            parsed.final_limit = parsed.top_k
        if parsed.candidate_limit is None:
            parsed.candidate_limit = parsed.top_k
        if parsed.candidate_min_vector_similarity is None:
            parsed.candidate_min_vector_similarity = parsed.min_vector_similarity
        return parsed


def load_ablation_configs(path: Path) -> list[dict[str, object]]:
    """Load and validate explicit, dev-only ablation configurations."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid ablation JSON: {path}") from exc
    if not isinstance(value, list) or not value:
        raise ValueError("ablation configuration must be a non-empty array")

    configs: list[dict[str, object]] = []
    names: set[str] = set()
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"ablation config {index} must be an object")
        unknown = set(item) - _ABLATION_FIELDS
        if unknown:
            raise ValueError(f"ablation config {index} has unknown fields: {sorted(unknown)}")
        required = _ABLATION_FIELDS - {"name"}
        if not required.issubset(item):
            missing = sorted(required - set(item))
            raise ValueError(f"ablation config {index} is missing fields: {missing}")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError(f"ablation config {index} has invalid or duplicate name")
        if item.get("split") != "dev":
            raise ValueError("ablation configurations may only use split=dev")
        if item.get("mode") not in {"keyword", "vector", "hybrid"}:
            raise ValueError(f"ablation config {name} has unsupported mode")
        candidate_limit = item.get("candidate_limit")
        final_limit = item.get("final_limit")
        if (
            isinstance(candidate_limit, bool)
            or not isinstance(candidate_limit, int)
            or candidate_limit <= 0
            or isinstance(final_limit, bool)
            or not isinstance(final_limit, int)
            or final_limit <= 0
            or candidate_limit < final_limit
        ):
            raise ValueError(f"ablation config {name} has invalid candidate/final limits")
        threshold = item.get("candidate_min_vector_similarity")
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
            raise ValueError(f"ablation config {name} has invalid candidate threshold")
        validate_min_vector_similarity(float(threshold))
        names.add(name)
        configs.append(dict(item))
    return configs


def select_ablation_config(
    configs: Sequence[dict[str, object]],
    *,
    name: str | None = None,
) -> dict[str, object]:
    if name is None:
        if len(configs) != 1:
            raise ValueError("--ablation-name is required when the config contains multiple strategies")
        return dict(configs[0])
    for config in configs:
        if config.get("name") == name:
            return dict(config)
    raise ValueError(f"ablation strategy not found: {name}")


def apply_ablation_config(args: argparse.Namespace, config: dict[str, object]) -> argparse.Namespace:
    """Apply a validated strategy to the actual evaluation arguments."""
    if args.split != "dev":
        raise ValueError("ablation configurations may only run with split=dev")
    for field in _ABLATION_FIELDS - {"name"}:
        setattr(args, field, config[field])
    args.ablation_name = config["name"]
    return args


def validate_min_vector_similarity(value: float) -> float:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("minimum vector similarity must be between 0 and 1")
    return value


def validate_report_output_path(output: Path, *, dataset: Path, database: Path) -> None:
    resolved_output = output.resolve()
    if resolved_output == dataset.resolve():
        raise ValueError("report output must not overwrite the evaluation dataset")
    if resolved_output == database.resolve():
        raise ValueError("report output must not overwrite the knowledge database")
    if resolved_output.exists():
        raise ValueError(f"report output already exists: {resolved_output}")


def judge_state_counts(results: Sequence[QuestionResult]) -> dict[str, dict[str, int]]:
    statuses: dict[str, int] = {
        "supported": 0,
        "insufficient": 0,
        "no_results": 0,
        "execution_error": 0,
    }
    reasons: dict[str, int] = {}
    for result in results:
        if result.status == QUESTION_STATUS_ERROR:
            statuses["execution_error"] += 1
            continue
        if result.judge_executed is not True:
            continue
        if result.judge_status in statuses:
            statuses[result.judge_status] += 1
        if result.judge_reason is not None:
            reasons[result.judge_reason] = reasons.get(result.judge_reason, 0) + 1
    return {"status": statuses, "reason": dict(sorted(reasons.items()))}


def _span_is_covered(result: SearchResult, span: RelevantSpan) -> bool:
    return (
        result.document_id == span.get("document_id")
        and result.document_version == span.get("document_version")
        and result.start_line <= span.get("start_line", 0)
        and result.end_line >= span.get("end_line", 0)
    )


def _covered_span_count(
    results: Sequence[SearchResult], spans: Sequence[RelevantSpan], k: int
) -> int:
    if k <= 0:
        return 0
    return sum(any(_span_is_covered(result, span) for result in results[:k]) for span in spans)


def recall_at_k(results: Sequence[SearchResult], spans: Sequence[RelevantSpan], *, k: int) -> float:
    """Return the fraction of annotated evidence spans covered by top-k results."""
    if not spans or k <= 0:
        return 0.0
    return _covered_span_count(results, spans, k) / len(spans)


def mean_reciprocal_rank(
    results: Sequence[SearchResult], spans: Sequence[RelevantSpan], *, k: int
) -> float:
    """Return reciprocal rank of the first result covering an annotated span."""
    if k <= 0:
        return 0.0
    for rank, result in enumerate(results[:k], start=1):
        if any(_span_is_covered(result, span) for span in spans):
            return 1.0 / rank
    return 0.0


def evaluate_retrieval(
    questions: Sequence[EvaluationQuestion],
    result_map: dict[str, Sequence[SearchResult]],
    *,
    k: int = 5,
    evidence_status_map: dict[str, str] | None = None,
    judge_executed_map: dict[str, bool] | None = None,
    llm_called_map: dict[str, bool] | None = None,
) -> EvaluationSummary:
    """Summarize a retrieval-only map without inferring model answers.

    Without an executed Judge, this helper intentionally reports no refusal
    quality. A non-empty retrieval result is only an observable retrieval fact,
    not an answer and not a successful refusal.
    """
    answerable = [question for question in questions if question.answerable]
    unanswerable = [question for question in questions if not question.answerable]
    recalls = [
        recall_at_k(result_map.get(question.question_id, ()), question.relevant_spans, k=k)
        for question in answerable
    ]
    mrrs = [
        mean_reciprocal_rank(
            result_map.get(question.question_id, ()), question.relevant_spans, k=k
        )
        for question in answerable
    ]
    evidence_status_map = evidence_status_map or {}
    judge_executed_map = judge_executed_map or {}
    llm_called_map = llm_called_map or {}
    correct_refusals = sum(
        judge_executed_map.get(question.question_id, False)
        and evidence_status_map.get(question.question_id) in {"insufficient", "no_results"}
        for question in unanswerable
    )
    incorrect_answers = sum(
        llm_called_map.get(question.question_id, False)
        and evidence_status_map.get(question.question_id) == "supported"
        for question in unanswerable
    )
    return EvaluationSummary(
        question_count=len(questions),
        answerable_count=len(answerable),
        recall_at_k=sum(recalls) / len(recalls) if recalls else 0.0,
        mrr_at_k=sum(mrrs) / len(mrrs) if mrrs else 0.0,
        unanswerable_count=len(unanswerable),
        correct_refusal_count=correct_refusals,
        incorrect_answer_count=incorrect_answers,
        unanswerable_nonempty_count=sum(
            bool(result_map.get(question.question_id)) for question in unanswerable
        ),
        incorrect_refusal_count=sum(
            judge_executed_map.get(question.question_id, False)
            and evidence_status_map.get(question.question_id) == "supported"
            for question in unanswerable
        ),
        refusal_evaluation=(
            "judge_only"
            if any(judge_executed_map.get(question.question_id, False) for question in unanswerable)
            else "retrieval_only"
        ),
    )


def _covered_span_indexes(
    result: SearchResult, spans: Sequence[RelevantSpan]
) -> list[int]:
    return [index for index, span in enumerate(spans) if _span_is_covered(result, span)]


def _source_coverage(
    results: Sequence[SearchResult], spans: Sequence[RelevantSpan]
) -> dict[str, int]:
    required_documents = {
        str(span["document_id"])
        for span in spans
        if isinstance(span.get("document_id"), str)
    }
    covered_documents = {
        result.document_id
        for result in results
        if any(_span_is_covered(result, span) for span in spans)
    }
    return {
        "covered": len(covered_documents.intersection(required_documents)),
        "required": len(required_documents),
    }


def _as_search_results(items: Sequence[EvidenceItem]) -> list[SearchResult]:
    return [item.result if isinstance(item, CandidateEvidence) else item for item in items]


def evaluate_question_results(
    questions: Sequence[EvaluationQuestion],
    result_map: dict[str, Sequence[EvidenceItem]],
    *,
    error_map: dict[str, str] | None = None,
    score_type: str = "unknown",
    latency_map: dict[str, float] | None = None,
    k: int = 5,
    candidate_result_map: dict[str, Sequence[EvidenceItem]] | None = None,
    final_result_map: dict[str, Sequence[EvidenceItem]] | None = None,
    candidate_score_type: str | None = None,
    final_score_type: str | None = None,
    evidence_status_map: dict[str, str] | None = None,
    phase_latency_map: dict[str, dict[str, float | None]] | None = None,
    source_coverage_map: dict[str, dict[str, int]] | None = None,
    candidate_limit: int | None = None,
    llm_called_map: dict[str, bool] | None = None,
    citation_complete_map: dict[str, bool] | None = None,
    query_plan_map: dict[str, dict[str, object]] | None = None,
    judge_reason_map: dict[str, str] | None = None,
    judge_executed_map: dict[str, bool] | None = None,
) -> list[QuestionResult]:
    """Build auditable per-question retrieval results.

    An execution error is deliberately different from an empty retrieval result:
    errors have no quality metric and never count as correct refusals.
    """
    if k <= 0:
        raise ValueError("evaluation k must be positive")
    candidate_limit = candidate_limit or k
    if candidate_limit <= 0:
        raise ValueError("evaluation candidate limit must be positive")
    if not score_type.strip():
        raise ValueError("score_type must be non-empty")
    error_map = error_map or {}
    latency_map = latency_map or {}
    candidate_result_map = result_map if candidate_result_map is None else candidate_result_map
    final_result_map = result_map if final_result_map is None else final_result_map
    candidate_score_type = score_type if candidate_score_type is None else candidate_score_type
    final_score_type = score_type if final_score_type is None else final_score_type
    evidence_status_map = {} if evidence_status_map is None else evidence_status_map
    phase_latency_map = {} if phase_latency_map is None else phase_latency_map
    source_coverage_map = {} if source_coverage_map is None else source_coverage_map
    llm_called_map = {} if llm_called_map is None else llm_called_map
    citation_complete_map = {} if citation_complete_map is None else citation_complete_map
    query_plan_map = {} if query_plan_map is None else query_plan_map
    judge_reason_map = {} if judge_reason_map is None else judge_reason_map
    judge_executed_map = {} if judge_executed_map is None else judge_executed_map
    results: list[QuestionResult] = []
    for question in questions:
        question_id = question.question_id
        candidate_results = list(candidate_result_map.get(question_id, ()))
        final_results = list(final_result_map.get(question_id, ()))
        retrieved = final_results
        error = error_map.get(question_id)
        if error is not None and not error.strip():
            raise ValueError(f"evaluation error must be non-empty: {question_id}")
        latency = latency_map.get(question_id)
        if latency is not None and (not math.isfinite(latency) or latency < 0):
            raise ValueError(f"evaluation latency must be finite and non-negative: {question_id}")

        def serialize(
            results_to_serialize: Sequence[EvidenceItem],
            result_score_type: str,
            result_limit: int,
        ) -> list[dict[str, object]]:
            serialized: list[dict[str, object]] = []
            for rank, item in enumerate(results_to_serialize[:result_limit], start=1):
                candidate = item if isinstance(item, CandidateEvidence) else None
                result = candidate.result if candidate is not None else item
                entry: dict[str, object] = {
                    "rank": rank,
                    "chunk_id": result.chunk_id,
                    "document_id": result.document_id,
                    "document_version": result.document_version,
                    "source_path": result.source_path,
                    "start_line": result.start_line,
                    "end_line": result.end_line,
                    "score": result.score,
                    "score_type": result_score_type,
                    "covered_span_indexes": _covered_span_indexes(
                        result, question.relevant_spans
                    ),
                }
                if candidate is not None:
                    entry["query_ids"] = list(candidate.query_ids)
                    entry["retrieval_ranks"] = list(candidate.retrieval_ranks)
                    entry["retrieval_score"] = result.score
                    entry["retrieval_score_type"] = candidate.retrieval_score_type
                    entry["rerank_score"] = candidate.rerank_score
                    if candidate.rerank_score is not None:
                        entry["score_type"] = result_score_type or "rerank"
                serialized.append(entry)
            return serialized

        serialized_candidate_results = serialize(candidate_results, candidate_score_type, candidate_limit)
        serialized_final_results = serialize(final_results, final_score_type, k)

        candidate_results_as_search = [
            item.result if isinstance(item, CandidateEvidence) else item
            for item in candidate_results
        ]
        final_results_as_search = [
            item.result if isinstance(item, CandidateEvidence) else item
            for item in final_results
        ]

        if error is not None:
            status = QUESTION_STATUS_ERROR
            recall = None
            mrr = None
            hit = None
        else:
            status = QUESTION_STATUS_SUCCESS if retrieved else QUESTION_STATUS_EMPTY
            if question.answerable:
                recall = recall_at_k(final_results_as_search, question.relevant_spans, k=k)
                mrr = mean_reciprocal_rank(final_results_as_search, question.relevant_spans, k=k)
                hit = any(
                    bool(item["covered_span_indexes"])
                    for item in serialized_final_results
                )
            else:
                recall = None
                mrr = None
                hit = None

        judge_status = evidence_status_map.get(question_id)
        judge_executed = judge_executed_map.get(question_id)
        if error is not None or question.answerable:
            correct_refusal = None
        elif judge_executed and judge_status in {"insufficient", "no_results"}:
            correct_refusal = judge_status in {"insufficient", "no_results"}
        else:
            # A supported Coverage Judge state is not a semantic refusal result.
            # Without an answerer call, refusal quality remains unevaluated.
            correct_refusal = None
        incorrect_refusal = (
            True
            if (
                error is None
                and not question.answerable
                and judge_executed
                and llm_called_map.get(question_id, False)
                and judge_status == "supported"
            )
            else None
        )

        results.append(
            QuestionResult(
                question_id=question_id,
                category=question.category,
                answerable=question.answerable,
                status=status,
                score_type=score_type,
                latency_ms=latency,
                recall_at_k=recall,
                mrr_at_k=mrr,
                hit_at_k=hit,
                error=error,
                retrieved_results=tuple(serialized_final_results),
                candidate_results=tuple(serialized_candidate_results),
                final_results=tuple(serialized_final_results),
                candidate_evidence_recall_at_k=(
                    None
                    if error is not None or not question.answerable
                    else recall_at_k(
                        candidate_results_as_search,
                        question.relevant_spans,
                        k=candidate_limit,
                    )
                ),
                candidate_mrr_at_k=(
                    None
                    if error is not None or not question.answerable
                    else mean_reciprocal_rank(
                        candidate_results_as_search,
                        question.relevant_spans,
                        k=candidate_limit,
                    )
                ),
                candidate_hit_at_k=(
                    None
                    if error is not None or not question.answerable
                    else any(
                        bool(item["covered_span_indexes"])
                        for item in serialized_candidate_results
                    )
                ),
                final_evidence_recall=(
                    None
                    if error is not None or not question.answerable
                    else recall_at_k(final_results_as_search, question.relevant_spans, k=k)
                ),
                source_coverage=source_coverage_map.get(question_id)
                or _source_coverage(
                    _as_search_results(final_results),
                    question.relevant_spans,
                ),
                evidence_status=evidence_status_map.get(question_id),
                judge_reason=judge_reason_map.get(question_id),
                judge_executed=judge_executed,
                phase_latency_ms=phase_latency_map.get(question_id),
                llm_called=llm_called_map.get(question_id),
                citation_complete=citation_complete_map.get(question_id),
                query_plan=query_plan_map.get(question_id),
                candidate_nonempty=bool(candidate_results),
                final_nonempty=bool(final_results),
                judge_status=judge_status,
                incorrect_refusal=incorrect_refusal,
                correct_refusal=correct_refusal,
            )
        )
    return results


def _latency_summary(results: Sequence[QuestionResult]) -> dict[str, float | int | None]:
    values = sorted(
        result.latency_ms
        for result in results
        if result.latency_ms is not None
    )
    if not values:
        return {
            "count": 0,
            "mean": None,
            "p50": None,
            "p95": None,
            "max": None,
        }

    def percentile(percent: float) -> float:
        index = min(len(values) - 1, max(0, math.ceil(percent * len(values)) - 1))
        return values[index]

    return {
        "count": len(values),
        "mean": sum(values) / len(values),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "max": max(values),
    }


def _summarize_question_results(
    results: Sequence[QuestionResult],
) -> QuestionResultSummary:
    answerable = [result for result in results if result.answerable]
    unanswerable = [result for result in results if not result.answerable]
    evaluated_answerable = [result for result in answerable if result.status != QUESTION_STATUS_ERROR]
    evaluated_unanswerable = [
        result for result in unanswerable if result.status != QUESTION_STATUS_ERROR
    ]
    recalls = [
        result.recall_at_k
        for result in evaluated_answerable
        if result.recall_at_k is not None
    ]
    mrrs = [
        result.mrr_at_k
        for result in evaluated_answerable
        if result.mrr_at_k is not None
    ]
    hits = [
        result.hit_at_k
        for result in evaluated_answerable
        if result.hit_at_k is not None
    ]
    candidate_recalls = [
        result.candidate_evidence_recall_at_k
        for result in evaluated_answerable
        if result.candidate_evidence_recall_at_k is not None
    ]
    candidate_mrrs = [
        result.candidate_mrr_at_k
        for result in evaluated_answerable
        if result.candidate_mrr_at_k is not None
    ]
    candidate_hits = [
        result.candidate_hit_at_k
        for result in evaluated_answerable
        if result.candidate_hit_at_k is not None
    ]
    final_recalls = [
        result.final_evidence_recall
        for result in evaluated_answerable
        if result.final_evidence_recall is not None
    ]
    source_coverages = [
        result.source_coverage
        for result in evaluated_answerable
        if result.source_coverage is not None
    ]
    phase_latency: dict[str, dict[str, float | int | None]] = {}
    phase_names = ("query_planner", "candidate", "rerank", "selection", "judge", "llm")
    for phase in phase_names:
        values = [
            result.phase_latency_ms[phase]
            for result in results
            if result.phase_latency_ms is not None
            and phase in result.phase_latency_ms
            and result.phase_latency_ms[phase] is not None
        ]
        if values:
            numeric_values = sorted(float(value) for value in values)
            phase_latency[phase] = {
                "count": len(numeric_values),
                "mean": sum(numeric_values) / len(numeric_values),
                "p50": numeric_values[min(len(numeric_values) - 1, len(numeric_values) // 2)],
                "p95": numeric_values[min(len(numeric_values) - 1, math.ceil(len(numeric_values) * 0.95) - 1)],
                "max": max(numeric_values),
            }
        else:
            phase_latency[phase] = {"count": 0, "mean": None, "p50": None, "p95": None, "max": None}
    return QuestionResultSummary(
        question_count=len(results),
        answerable_count=len(answerable),
        evaluated_answerable_count=len(evaluated_answerable),
        recall_at_k=sum(recalls) / len(recalls) if recalls else None,
        mrr_at_k=sum(mrrs) / len(mrrs) if mrrs else None,
        hit_at_k=sum(hits) / len(hits) if hits else None,
        answerable_empty_result_count=sum(
            result.status == QUESTION_STATUS_EMPTY for result in answerable
        ),
        unanswerable_count=len(unanswerable),
        evaluated_unanswerable_count=len(evaluated_unanswerable),
        unanswerable_empty_result_count=sum(
            result.status == QUESTION_STATUS_EMPTY for result in unanswerable
        ),
        correct_refusal_count=sum(result.correct_refusal is True for result in unanswerable),
        incorrect_answer_count=sum(
            result.llm_called is True
            and result.judge_status == "supported"
            for result in unanswerable
        ),
        incorrect_refusal_count=sum(
            result.incorrect_refusal is True for result in unanswerable
        ),
        execution_error_count=sum(
            result.status == QUESTION_STATUS_ERROR for result in results
        ),
        retrieval_latency_ms=_latency_summary(results),
        candidate_evidence_recall_at_k=(
            sum(candidate_recalls) / len(candidate_recalls) if candidate_recalls else None
        ),
        candidate_mrr_at_k=(
            sum(candidate_mrrs) / len(candidate_mrrs) if candidate_mrrs else None
        ),
        candidate_hit_at_k=(
            sum(candidate_hits) / len(candidate_hits) if candidate_hits else None
        ),
        final_evidence_recall=sum(final_recalls) / len(final_recalls) if final_recalls else None,
        source_coverage=(
            {
                "covered": sum(item["covered"] for item in source_coverages),
                "required": sum(item["required"] for item in source_coverages),
            }
            if source_coverages
            else None
        ),
        unanswerable_nonempty_result_count=sum(
            result.final_nonempty and result.status != QUESTION_STATUS_ERROR
            for result in unanswerable
        ),
        unanswerable_candidate_nonempty_result_count=sum(
            result.candidate_nonempty and result.status != QUESTION_STATUS_ERROR
            for result in unanswerable
        ),
        phase_latency_ms=phase_latency,
        candidate_nonempty_count=sum(
            result.candidate_nonempty and result.status != QUESTION_STATUS_ERROR
            for result in results
        ),
        final_nonempty_count=sum(
            result.final_nonempty and result.status != QUESTION_STATUS_ERROR
            for result in results
        ),
        judge_executed_count=sum(
            result.judge_executed is True and result.status != QUESTION_STATUS_ERROR
            for result in results
        ),
        judge_status_counts=judge_state_counts(results)["status"],
        judge_reason_counts=judge_state_counts(results)["reason"],
    )


def summarize_question_results(
    results: Sequence[QuestionResult],
    *,
    k: int = 5,
    group_by: str | None = None,
) -> QuestionResultSummary | dict[str, QuestionResultSummary]:
    """Summarize detailed results overall or by category/answerability."""
    if k <= 0:
        raise ValueError("evaluation k must be positive")
    if group_by is None:
        return _summarize_question_results(results)
    if group_by not in {"category", "answerable"}:
        raise ValueError("group_by must be category or answerable")
    grouped: dict[str, list[QuestionResult]] = {}
    for result in results:
        key = (
            result.category
            if group_by == "category"
            else "answerable" if result.answerable else "unanswerable"
        )
        grouped.setdefault(key, []).append(result)
    return {
        key: _summarize_question_results(grouped[key])
        for key in sorted(grouped)
    }


def build_014_trace(
    questions: Sequence[EvaluationQuestion],
    question_results: Sequence[QuestionResult],
) -> dict[str, object] | None:
    """Return the auditable 014 row without inferring unobserved model behavior."""
    question = next((item for item in questions if item.question_id == "blog-formal-014"), None)
    result = next((item for item in question_results if item.question_id == "blog-formal-014"), None)
    if question is None or result is None:
        return None
    required_documents = sorted(
        {
            str(span["document_id"])
            for span in question.relevant_spans
            if isinstance(span.get("document_id"), str)
        }
    )
    candidate_documents = sorted(
        {str(item["document_id"]) for item in result.candidate_results}
    )
    final_documents = sorted({str(item["document_id"]) for item in result.final_results})
    candidate_span_indexes = sorted(
        {
            index
            for item in result.candidate_results
            for index in item.get("covered_span_indexes", [])
            if isinstance(index, int)
        }
    )
    final_span_indexes = sorted(
        {
            index
            for item in result.final_results
            for index in item.get("covered_span_indexes", [])
            if isinstance(index, int)
        }
    )
    return {
        "question_id": question.question_id,
        "original_query_preserved": bool(
            result.query_plan
            and result.query_plan.get("original_query") == question.query
        ),
        "query_plan": result.query_plan,
        "candidate_evidence": list(result.candidate_results),
        "final_evidence": list(result.final_results),
        "candidate_span_indexes": candidate_span_indexes,
        "final_span_indexes": final_span_indexes,
        "candidate_documents": candidate_documents,
        "final_documents": final_documents,
        "required_documents": required_documents,
        "source_coverage": result.source_coverage,
        "evidence_status": result.evidence_status,
        "judge_reason": result.judge_reason,
        "judge_executed": result.judge_executed,
        "judge_status": result.judge_status,
        "candidate_nonempty": result.candidate_nonempty,
        "final_nonempty": result.final_nonempty,
        "correct_refusal": result.correct_refusal,
        "incorrect_refusal": result.incorrect_refusal,
        "llm_called": result.llm_called,
        "citation_complete": result.citation_complete,
        "execution_status": result.status,
        "error": result.error,
    }


def _serialize_query_plan(pipeline_result: PipelineResult) -> dict[str, object]:
    return {
        "original_query": pipeline_result.plan.original_query,
        "is_multi_evidence": pipeline_result.plan.is_multi_evidence,
        "queries": [
            {
                "query_id": query.query_id,
                "text": query.text,
                "facet": query.facet,
            }
            for query in pipeline_result.plan.queries
        ],
    }


def load_questions(path: Path, *, split: str) -> list[EvaluationQuestion]:
    questions: list[EvaluationQuestion] = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on line {line_number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"question on line {line_number} must be an object")
        if value.get("split") != split:
            continue
        question_id = value.get("id")
        query = value.get("query")
        category = value.get("category")
        record_split = value.get("split")
        answerable = value.get("answerable")
        spans = value.get("relevant_spans")
        reference_answer = value.get("reference_answer")
        if (
            not isinstance(question_id, str)
            or not question_id
            or question_id in seen_ids
            or not isinstance(query, str)
            or not query.strip()
            or not isinstance(category, str)
            or record_split != split
            or not isinstance(answerable, bool)
            or not isinstance(spans, list)
            or not isinstance(reference_answer, str)
        ):
            raise ValueError(f"invalid question fields on line {line_number}")
        seen_ids.add(question_id)
        questions.append(
            EvaluationQuestion(
                question_id=question_id,
                query=query,
                category=category,
                split=record_split,
                answerable=answerable,
                relevant_spans=tuple(spans),
                reference_answer=reference_answer,
            )
        )
    return questions


async def _run(args: argparse.Namespace) -> int:
    validate_min_vector_similarity(args.min_vector_similarity)
    validate_min_vector_similarity(args.candidate_min_vector_similarity)
    if args.top_k <= 0 or args.final_limit <= 0 or args.candidate_limit <= 0:
        raise ValueError("evaluation limits must be positive")
    if args.candidate_limit < args.final_limit:
        raise ValueError("candidate_limit must not be smaller than final_limit")
    if args.query_planning not in {"disabled", "conditional"}:
        raise ValueError(f"unsupported query planning strategy: {args.query_planning}")
    if args.rerank not in {"noop", "fake-or-approved-provider"}:
        raise ValueError(f"unsupported rerank strategy: {args.rerank}")
    if args.evidence_selection not in {"baseline", "coverage-aware"}:
        raise ValueError(f"unsupported evidence selection strategy: {args.evidence_selection}")
    if args.answerability not in {"baseline", "coverage-v1"}:
        raise ValueError(f"unsupported answerability strategy: {args.answerability}")
    dataset_path = Path(args.dataset)
    questions = load_questions(dataset_path, split=args.split)
    database_path = Path(args.database)
    if not database_path.is_file():
        raise ValueError(f"knowledge database does not exist: {database_path}")
    output_path = Path(args.output)
    validate_report_output_path(output_path, dataset=dataset_path, database=database_path)
    if args.mode in {"vector", "hybrid"} and not settings.embedding_api_key:
        raise ValueError("vector and hybrid modes require a configured embedding API key")
    repository = KnowledgeRepository(database_path, read_only=True)
    await repository.init()

    embedding_client: EmbeddingClient | None = None
    if args.mode in {"vector", "hybrid"}:
        if not args.embedding_model or args.embedding_dimensions <= 0:
            raise ValueError("vector and hybrid modes require embedding model and dimensions")
        embedding_client = EmbeddingClient(
            api_key=settings.embedding_api_key,
            model=args.embedding_model,
            dimensions=args.embedding_dimensions,
            base_url=settings.embedding_base_url,
            timeout_seconds=settings.embedding_timeout_seconds,
        )

    planner = (
        SafeQueryPlanner()
        if args.query_planning == "disabled"
        else LLMQueryPlanner(QueryPlannerConfig(enabled=True))
    )
    candidate_retriever = RepositoryCandidateRetriever(
        repository,
        query_embedder=embedding_client.embed if embedding_client is not None else None,
        embedding_model=args.embedding_model,
        embedding_dimensions=args.embedding_dimensions,
        candidate_min_vector_similarity=args.candidate_min_vector_similarity,
    )
    reranker = (
        NoopReranker()
        if args.rerank == "noop"
        else OfflineFakeReranker()
    )
    selector = (
        BaselineEvidenceSelector()
        if args.evidence_selection == "baseline"
        else CoverageAwareEvidenceSelector()
    )
    judge = (
        BaselineAnswerabilityJudge()
        if args.answerability == "baseline"
        else CoverageAnswerabilityJudge()
    )
    pipeline = KnowledgePipeline(
        planner=planner,
        candidate_retriever=candidate_retriever,
        reranker=reranker,
        selector=selector,
        judge=judge,
        answerability_config=AnswerabilityConfig(
            min_supported_coverage=settings.knowledge_answerability_min_supported_coverage,
            min_partial_coverage=settings.knowledge_answerability_min_partial_coverage,
            min_supported_evidence=settings.knowledge_answerability_min_supported_evidence,
            multi_evidence_requires_all_queries=(
                settings.knowledge_answerability_multi_evidence_requires_all_queries
            ),
            allow_insufficient_llm=settings.knowledge_answerability_allow_insufficient_llm,
        ),
    )

    result_map: dict[str, list[EvidenceItem]] = {}
    candidate_result_map: dict[str, list[EvidenceItem]] = {}
    final_result_map: dict[str, list[EvidenceItem]] = {}
    error_map: dict[str, str] = {}
    latency_map: dict[str, float] = {}
    phase_latency_map: dict[str, dict[str, float | None]] = {}
    llm_called_map: dict[str, bool] = {}
    judge_executed_map: dict[str, bool] = {}
    evidence_status_map: dict[str, str] = {}
    judge_reason_map: dict[str, str] = {}
    query_plan_map: dict[str, dict[str, object]] = {}
    source_coverage_map: dict[str, dict[str, int]] = {}
    for question in questions:
        started = time.perf_counter()
        try:
            pipeline_result = await pipeline.run(
                question.query,
                mode=args.mode,
                candidate_limit=args.candidate_limit,
                final_limit=args.final_limit,
                # Offline evaluation does not call an Answerer or an external
                # planning model. The configured planner still executes and
                # safely falls back to the original query when no LLM exists.
                llm_client=None,
            )
            candidates = list(pipeline_result.candidates)
            final_results = list(pipeline_result.selection.selected)
            result_map[question.question_id] = final_results
            candidate_result_map[question.question_id] = candidates
            final_result_map[question.question_id] = final_results
            evidence_status_map[question.question_id] = pipeline_result.decision.status
            judge_reason_map[question.question_id] = pipeline_result.decision.reason
            query_plan_map[question.question_id] = _serialize_query_plan(pipeline_result)
            source_coverage_map[question.question_id] = _source_coverage(
                [candidate.result for candidate in final_results],
                question.relevant_spans,
            )
            phase_latency_map[question.question_id] = {
                "query_planner": pipeline_result.query_planner_latency_ms,
                "candidate": pipeline_result.candidate_latency_ms,
                "rerank": pipeline_result.rerank_latency_ms,
                "selection": pipeline_result.selection_latency_ms,
                "judge": pipeline_result.judge_latency_ms,
                "llm": None,
            }
            judge_executed_map[question.question_id] = True
        except Exception as exc:
            error_map[question.question_id] = str(exc) or exc.__class__.__name__
            result_map[question.question_id] = []
            candidate_result_map[question.question_id] = []
            final_result_map[question.question_id] = []
            judge_executed_map[question.question_id] = False
            phase_latency_map[question.question_id] = {
                "query_planner": None,
                "candidate": None,
                "rerank": None,
                "selection": None,
                "judge": None,
                "llm": None,
            }
        latency = (time.perf_counter() - started) * 1000
        latency_map[question.question_id] = latency
        llm_called_map[question.question_id] = False
    score_type = {
        "keyword": "bm25",
        "vector": "cosine",
        "hybrid": "rrf",
    }[args.mode]
    final_score_type = "rerank" if args.rerank != "noop" else score_type
    question_results = evaluate_question_results(
        questions,
        result_map,
        candidate_result_map=candidate_result_map,
        final_result_map=final_result_map,
        error_map=error_map,
        score_type=score_type,
        latency_map=latency_map,
        k=args.final_limit,
        candidate_limit=args.candidate_limit,
        candidate_score_type=score_type,
        final_score_type=final_score_type,
        phase_latency_map=phase_latency_map,
        llm_called_map=llm_called_map,
        judge_executed_map=judge_executed_map,
        evidence_status_map=evidence_status_map,
        judge_reason_map=judge_reason_map,
        query_plan_map=query_plan_map,
        source_coverage_map=source_coverage_map,
    )
    detailed_summary = summarize_question_results(question_results, k=args.final_limit)
    summary = EvaluationSummary(
        question_count=detailed_summary.question_count,
        answerable_count=detailed_summary.answerable_count,
        recall_at_k=detailed_summary.recall_at_k,
        mrr_at_k=detailed_summary.mrr_at_k,
        unanswerable_count=detailed_summary.unanswerable_count,
        correct_refusal_count=detailed_summary.correct_refusal_count,
        incorrect_answer_count=detailed_summary.incorrect_answer_count,
        unanswerable_nonempty_count=detailed_summary.unanswerable_nonempty_result_count,
        incorrect_refusal_count=detailed_summary.incorrect_refusal_count,
        refusal_evaluation=(
            "coverage_judge_state_only"
            if args.answerability == "coverage-v1"
            else "retrieval_only"
        ),
    )
    report = {
        **asdict(summary),
        "recall_at_5": summary.recall_at_k if args.final_limit == 5 else None,
        "mrr_at_5": summary.mrr_at_k if args.final_limit == 5 else None,
        "metrics_version": "pipeline-evaluation-v3",
        "status": (
            "incomplete"
            if detailed_summary.execution_error_count
            else "complete"
        ),
        "execution_error_count": detailed_summary.execution_error_count,
        "evaluated_answerable_count": detailed_summary.evaluated_answerable_count,
        "hit_at_5": detailed_summary.hit_at_k if args.final_limit == 5 else None,
        "answerable_empty_result_count": detailed_summary.answerable_empty_result_count,
        "unanswerable_empty_result_count": detailed_summary.unanswerable_empty_result_count,
        "unanswerable_nonempty_result_count": detailed_summary.unanswerable_nonempty_result_count,
        "unanswerable_candidate_nonempty_result_count": detailed_summary.unanswerable_candidate_nonempty_result_count,
        "refusal_evaluation": summary.refusal_evaluation,
        "evaluated_unanswerable_count": detailed_summary.evaluated_unanswerable_count,
        "candidate_nonempty_count": detailed_summary.candidate_nonempty_count,
        "final_nonempty_count": detailed_summary.final_nonempty_count,
        "judge_executed_count": detailed_summary.judge_executed_count,
        "mode": args.mode,
        "split": args.split,
        "top_k": args.top_k,
        "final_limit": args.final_limit,
        "candidate_limit": args.candidate_limit,
        "candidate_min_vector_similarity": args.candidate_min_vector_similarity,
        "query_planning": args.query_planning,
        "rerank": args.rerank,
        "evidence_selection": args.evidence_selection,
        "answerability": args.answerability,
        "answerability_config": {
            "min_supported_coverage": settings.knowledge_answerability_min_supported_coverage,
            "min_partial_coverage": settings.knowledge_answerability_min_partial_coverage,
            "min_supported_evidence": settings.knowledge_answerability_min_supported_evidence,
            "multi_evidence_requires_all_queries": (
                settings.knowledge_answerability_multi_evidence_requires_all_queries
            ),
            "allow_insufficient_llm": settings.knowledge_answerability_allow_insufficient_llm,
        },
        "ablation_name": getattr(args, "ablation_name", None),
        "dataset_sha256": hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest(),
        "database": str(Path(args.database)),
        "database_sha256": hashlib.sha256(database_path.read_bytes()).hexdigest(),
        "embedding_model": args.embedding_model or None,
        "embedding_dimensions": args.embedding_dimensions or None,
        "min_vector_similarity": (
            args.min_vector_similarity if args.mode in {"vector", "hybrid"} else None
        ),
        "retrieval_latency_ms": detailed_summary.retrieval_latency_ms,
        "phase_latency_ms": detailed_summary.phase_latency_ms,
        "candidate_evidence_recall_at_k": detailed_summary.candidate_evidence_recall_at_k,
        "candidate_mrr_at_k": detailed_summary.candidate_mrr_at_k,
        "candidate_hit_at_k": detailed_summary.candidate_hit_at_k,
        "final_evidence_recall": detailed_summary.final_evidence_recall,
        "source_coverage": detailed_summary.source_coverage,
        "refusal_policy": (
            "coverage_judge_state_only_not_semantic_verification"
            if args.answerability == "coverage-v1"
            else "not_evaluated_in_baseline_judge"
        ),
        "judge_state_counts": judge_state_counts(question_results),
        "execution": {
            "query_planner_executed": any(
                result.phase_latency_ms is not None
                and result.phase_latency_ms.get("query_planner") is not None
                for result in question_results
            ),
            "candidate_retriever_executed": any(
                result.phase_latency_ms is not None
                and result.phase_latency_ms.get("candidate") is not None
                for result in question_results
            ),
            "reranker_executed": any(
                result.phase_latency_ms is not None
                and result.phase_latency_ms.get("rerank") is not None
                for result in question_results
            ),
            "evidence_selector_executed": any(
                result.phase_latency_ms is not None
                and result.phase_latency_ms.get("selection") is not None
                for result in question_results
            ),
            "judge_executed": any(
                result.judge_executed is True for result in question_results
            ),
            "llm_executed": False,
            "answer_quality_evaluated": False,
        },
        "by_category": {
            key: asdict(value)
            for key, value in summarize_question_results(
                question_results,
                k=args.final_limit,
                group_by="category",
            ).items()
        },
        "by_answerability": {
            key: asdict(value)
            for key, value in summarize_question_results(
                question_results,
                k=args.final_limit,
                group_by="answerable",
            ).items()
        },
        "question_results": [asdict(result) for result in question_results],
        "blog_formal_014": build_014_trace(questions, question_results),
        "answer_quality": "not evaluated by offline retrieval CLI",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8") as report_file:
        report_file.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = _EvaluationArgumentParser(description="Evaluate Tomato Agent knowledge retrieval")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--split", choices=("dev", "test"), required=True)
    parser.add_argument("--mode", choices=("keyword", "vector", "hybrid"), required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--database", default=str(settings.database_path))
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--final-limit", type=int, default=None)
    parser.add_argument("--candidate-limit", type=int, default=None)
    parser.add_argument("--embedding-model", default=settings.embedding_model)
    parser.add_argument("--embedding-dimensions", type=int, default=settings.embedding_dimensions)
    parser.add_argument(
        "--min-vector-similarity",
        type=lambda value: validate_min_vector_similarity(float(value)),
        default=validate_min_vector_similarity(settings.knowledge_min_vector_similarity),
    )
    parser.add_argument(
        "--candidate-min-vector-similarity",
        type=lambda value: validate_min_vector_similarity(float(value)),
        default=None,
    )
    parser.add_argument("--query-planning", choices=("disabled", "conditional"), default="disabled")
    parser.add_argument("--rerank", default="noop")
    parser.add_argument("--evidence-selection", default="baseline")
    parser.add_argument("--answerability", default="baseline")
    parser.set_defaults(final_limit=None)
    parser.add_argument(
        "--ablation-config",
        type=Path,
        help="apply a dev-only ablation configuration before running",
    )
    parser.add_argument("--ablation-name", help="strategy name inside --ablation-config")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.final_limit is None:
            args.final_limit = args.top_k
        if args.ablation_config is not None:
            configs = load_ablation_configs(args.ablation_config)
            apply_ablation_config(
                args,
                select_ablation_config(configs, name=args.ablation_name),
            )
        return asyncio.run(_run(args))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())