"""Offline retrieval evaluation and reproducible command-line reporting."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable, Sequence

from app.knowledge.embeddings import EmbeddingClient
from app.knowledge.models import SearchResult
from app.knowledge.repository import KnowledgeRepository
from app.settings import settings

RelevantSpan = dict[str, object]
SearchFunction = Callable[[str, int], Awaitable[list[SearchResult]]]


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
    final_evidence_recall: float | None = None
    source_coverage: dict[str, int] | None = None
    evidence_status: str | None = None
    judge_reason: str | None = None
    judge_executed: bool | None = None
    phase_latency_ms: dict[str, float | None] | None = None
    llm_called: bool | None = None
    citation_complete: bool | None = None
    query_plan: dict[str, object] | None = None


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
    final_evidence_recall: float | None = None
    source_coverage: dict[str, int] | None = None
    unanswerable_nonempty_result_count: int = 0
    unanswerable_candidate_nonempty_result_count: int = 0
    phase_latency_ms: dict[str, dict[str, float | int | None]] | None = None


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


def validate_min_vector_similarity(value: float) -> float:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("minimum vector similarity must be between 0 and 1")
    return value


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
) -> EvaluationSummary:
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
    correct_refusals = sum(not result_map.get(question.question_id) for question in unanswerable)
    return EvaluationSummary(
        question_count=len(questions),
        answerable_count=len(answerable),
        recall_at_k=sum(recalls) / len(recalls) if recalls else 0.0,
        mrr_at_k=sum(mrrs) / len(mrrs) if mrrs else 0.0,
        unanswerable_count=len(unanswerable),
        correct_refusal_count=correct_refusals,
        incorrect_answer_count=len(unanswerable) - correct_refusals,
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


def evaluate_question_results(
    questions: Sequence[EvaluationQuestion],
    result_map: dict[str, Sequence[SearchResult]],
    *,
    error_map: dict[str, str] | None = None,
    score_type: str = "unknown",
    latency_map: dict[str, float] | None = None,
    k: int = 5,
    candidate_result_map: dict[str, Sequence[SearchResult]] | None = None,
    final_result_map: dict[str, Sequence[SearchResult]] | None = None,
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
    candidate_result_map = candidate_result_map or result_map
    final_result_map = final_result_map or result_map
    candidate_score_type = candidate_score_type or score_type
    final_score_type = final_score_type or score_type
    evidence_status_map = evidence_status_map or {}
    phase_latency_map = phase_latency_map or {}
    source_coverage_map = source_coverage_map or {}
    llm_called_map = llm_called_map or {}
    citation_complete_map = citation_complete_map or {}
    query_plan_map = query_plan_map or {}
    judge_reason_map = judge_reason_map or {}
    judge_executed_map = judge_executed_map or {}
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
            results_to_serialize: Sequence[SearchResult],
            result_score_type: str,
            result_limit: int,
        ) -> list[dict[str, object]]:
            serialized: list[dict[str, object]] = []
            for rank, result in enumerate(results_to_serialize[:result_limit], start=1):
                serialized.append(
                {
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
            )
            return serialized

        serialized_candidate_results = serialize(candidate_results, candidate_score_type, candidate_limit)
        serialized_final_results = serialize(final_results, final_score_type, k)

        if error is not None:
            status = QUESTION_STATUS_ERROR
            recall = None
            mrr = None
            hit = None
        else:
            status = QUESTION_STATUS_SUCCESS if retrieved else QUESTION_STATUS_EMPTY
            if question.answerable:
                recall = recall_at_k(retrieved, question.relevant_spans, k=k)
                mrr = mean_reciprocal_rank(retrieved, question.relevant_spans, k=k)
                hit = any(
                    bool(item["covered_span_indexes"])
                    for item in serialized_final_results
                )
            else:
                recall = None
                mrr = None
                hit = None

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
                    else recall_at_k(candidate_results, question.relevant_spans, k=candidate_limit)
                ),
                final_evidence_recall=(
                    None
                    if error is not None or not question.answerable
                    else recall_at_k(final_results, question.relevant_spans, k=k)
                ),
                source_coverage=source_coverage_map.get(question_id)
                or _source_coverage(final_results, question.relevant_spans),
                evidence_status=evidence_status_map.get(question_id),
                judge_reason=judge_reason_map.get(question_id),
                judge_executed=judge_executed_map.get(question_id),
                phase_latency_ms=phase_latency_map.get(question_id),
                llm_called=llm_called_map.get(question_id),
                citation_complete=citation_complete_map.get(question_id),
                query_plan=query_plan_map.get(question_id),
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
        correct_refusal_count=sum(
            result.status == QUESTION_STATUS_EMPTY for result in unanswerable
        ),
        incorrect_answer_count=sum(
            result.status == QUESTION_STATUS_SUCCESS for result in unanswerable
        ),
        execution_error_count=sum(
            result.status == QUESTION_STATUS_ERROR for result in results
        ),
        retrieval_latency_ms=_latency_summary(results),
        candidate_evidence_recall_at_k=(
            sum(candidate_recalls) / len(candidate_recalls) if candidate_recalls else None
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
            result.status == QUESTION_STATUS_SUCCESS for result in unanswerable
        ),
        unanswerable_candidate_nonempty_result_count=sum(
            bool(result.candidate_results) and result.status != QUESTION_STATUS_ERROR
            for result in unanswerable
        ),
        phase_latency_ms=phase_latency,
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
        "original_query_preserved": True,
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
        "llm_called": result.llm_called,
        "citation_complete": result.citation_complete,
        "execution_status": result.status,
        "error": result.error,
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
            or not isinstance(record_split, str)
            or record_split not in {"dev", "test"}
            or not isinstance(answerable, bool)
            or not isinstance(spans, list)
            or not isinstance(reference_answer, str)
        ):
            raise ValueError(f"invalid question fields on line {line_number}")
        seen_ids.add(question_id)
        if record_split == split:
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
    questions = load_questions(Path(args.dataset), split=args.split)
    database_path = Path(args.database)
    if not database_path.is_file():
        raise ValueError(f"knowledge database does not exist: {database_path}")
    repository = KnowledgeRepository(database_path)
    await repository.init()

    async def search(query: str, limit: int) -> list[SearchResult]:
        if args.mode == "keyword":
            return await repository.search_chunks(query, limit=limit)
        if not args.embedding_model or args.embedding_dimensions <= 0:
            raise ValueError("vector and hybrid modes require embedding model and dimensions")
        client = EmbeddingClient(
            api_key=settings.embedding_api_key,
            model=args.embedding_model,
            dimensions=args.embedding_dimensions,
            base_url=settings.embedding_base_url,
            timeout_seconds=settings.embedding_timeout_seconds,
        )
        vector = (await client.embed([query]))[0]
        if args.mode == "vector":
            return await repository.search_vector_chunks(
                vector,
                model=args.embedding_model,
                dimensions=args.embedding_dimensions,
                limit=limit,
                min_score=args.candidate_min_vector_similarity,
            )
        return await repository.search_hybrid_chunks(
            query,
            vector,
            model=args.embedding_model,
            dimensions=args.embedding_dimensions,
            limit=limit,
            min_vector_score=args.candidate_min_vector_similarity,
        )

    result_map: dict[str, list[SearchResult]] = {}
    error_map: dict[str, str] = {}
    latency_map: dict[str, float] = {}
    phase_latency_map: dict[str, dict[str, float | None]] = {}
    llm_called_map: dict[str, bool] = {}
    judge_executed_map: dict[str, bool] = {}
    for question in questions:
        started = time.perf_counter()
        try:
            result_map[question.question_id] = await search(question.query, args.candidate_limit)
        except Exception as exc:
            error_map[question.question_id] = str(exc) or exc.__class__.__name__
            result_map[question.question_id] = []
        latency = (time.perf_counter() - started) * 1000
        latency_map[question.question_id] = latency
        phase_latency_map[question.question_id] = {
            "query_planner": None,
            "candidate": latency,
            "rerank": None,
            "selection": None,
            "judge": None,
            "llm": None,
        }
        llm_called_map[question.question_id] = False
        judge_executed_map[question.question_id] = False
    score_type = {
        "keyword": "bm25",
        "vector": "cosine",
        "hybrid": "rrf",
    }[args.mode]
    question_results = evaluate_question_results(
        questions,
        {question_id: results[: args.final_limit] for question_id, results in result_map.items()},
        candidate_result_map=result_map,
        final_result_map={question_id: results[: args.final_limit] for question_id, results in result_map.items()},
        error_map=error_map,
        score_type=score_type,
        latency_map=latency_map,
        k=args.final_limit,
        candidate_limit=args.candidate_limit,
        candidate_score_type=score_type,
        final_score_type=score_type,
        phase_latency_map=phase_latency_map,
        llm_called_map=llm_called_map,
        judge_executed_map=judge_executed_map,
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
    )
    report = {
        **asdict(summary),
        "recall_at_5": summary.recall_at_k if args.final_limit == 5 else None,
        "mrr_at_5": summary.mrr_at_k if args.final_limit == 5 else None,
        "metrics_version": "retrieval-v2",
        "status": "incomplete" if detailed_summary.execution_error_count else "complete",
        "execution_error_count": detailed_summary.execution_error_count,
        "evaluated_answerable_count": detailed_summary.evaluated_answerable_count,
        "hit_at_5": detailed_summary.hit_at_k if args.final_limit == 5 else None,
        "answerable_empty_result_count": detailed_summary.answerable_empty_result_count,
        "unanswerable_empty_result_count": detailed_summary.unanswerable_empty_result_count,
        "unanswerable_nonempty_result_count": detailed_summary.unanswerable_nonempty_result_count,
        "unanswerable_candidate_nonempty_result_count": detailed_summary.unanswerable_candidate_nonempty_result_count,
        "evaluated_unanswerable_count": detailed_summary.evaluated_unanswerable_count,
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
        "dataset_sha256": hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest(),
        "database": str(Path(args.database)),
        "embedding_model": args.embedding_model or None,
        "embedding_dimensions": args.embedding_dimensions or None,
        "min_vector_similarity": (
            args.min_vector_similarity if args.mode in {"vector", "hybrid"} else None
        ),
        "retrieval_latency_ms": detailed_summary.retrieval_latency_ms,
        "phase_latency_ms": detailed_summary.phase_latency_ms,
        "candidate_evidence_recall_at_k": detailed_summary.candidate_evidence_recall_at_k,
        "final_evidence_recall": detailed_summary.final_evidence_recall,
        "source_coverage": detailed_summary.source_coverage,
        "execution": {
            "query_planner_executed": False,
            "judge_executed": False,
            "llm_executed": False,
            "answer_quality_evaluated": False,
        },
        "by_category": {
            key: asdict(value)
            for key, value in summarize_question_results(
                question_results,
                k=args.top_k,
                group_by="category",
            ).items()
        },
        "by_answerability": {
            key: asdict(value)
            for key, value in summarize_question_results(
                question_results,
                k=args.top_k,
                group_by="answerable",
            ).items()
        },
        "question_results": [asdict(result) for result in question_results],
        "blog_formal_014": build_014_trace(questions, question_results),
        "answer_quality": "not evaluated by offline retrieval CLI",
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
        help="validate a dev-only ablation configuration before running",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.final_limit is None:
            args.final_limit = args.top_k
        if args.ablation_config is not None:
            load_ablation_configs(args.ablation_config)
        return asyncio.run(_run(args))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())