"""Per-question metrics and summaries for retrieval benchmarks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.knowledge.benchmark_models import EvaluationSummary, MetricValue, QuestionResult
from app.knowledge.evaluation import _span_is_covered
from app.knowledge.models import SearchResult

METRIC_VERSION = "retrieval-metrics/v1"


def _metric(values: Sequence[float], denominator: int | None = None) -> MetricValue:
    sample_count = len(values) if denominator is None else denominator
    if sample_count == 0:
        return MetricValue(value=None, sample_count=0)
    return MetricValue(value=sum(values) / sample_count, sample_count=sample_count)


def _summarize(results: Sequence[QuestionResult]) -> dict[str, MetricValue]:
    successful = [item for item in results if item.status == "complete"]
    answerable = [item for item in successful if item.answerable]
    unanswerable = [item for item in successful if not item.answerable]
    errors = sum(item.status == "error" for item in results)

    return {
        "execution_error_rate": _metric([1.0] * errors, denominator=len(results)),
        "answerable_empty_result_rate": _metric(
            [float(not item.retrieved_chunk_ids) for item in answerable]
        ),
        "unanswerable_empty_result_rate": _metric(
            [float(not item.retrieved_chunk_ids) for item in unanswerable]
        ),
        "unanswerable_nonempty_rate": _metric(
            [float(bool(item.retrieved_chunk_ids)) for item in unanswerable]
        ),
        "recall_at_1": _metric([float(item.recall_at_1) for item in successful if item.answerable and item.recall_at_1 is not None]),
        "recall_at_3": _metric([float(item.recall_at_3) for item in successful if item.answerable and item.recall_at_3 is not None]),
        "recall_at_5": _metric([float(item.recall_at_5) for item in successful if item.answerable and item.recall_at_5 is not None]),
        "mrr_at_5": _metric([float(item.reciprocal_rank) for item in successful if item.answerable and item.reciprocal_rank is not None]),
        "hit_at_5": _metric([float(item.hit) for item in successful if item.answerable and item.hit is not None]),
    }


def evaluate_questions(
    questions: Sequence[Mapping[str, Any]],
    result_map: Mapping[str, Sequence[SearchResult] | BaseException],
    *,
    score_type: str,
) -> tuple[tuple[QuestionResult, ...], EvaluationSummary]:
    """Evaluate one split; an exception result is recorded, never treated as empty."""
    results: list[QuestionResult] = []
    for question in questions:
        question_id = str(question["id"])
        spans = tuple(question["relevant_spans"])
        answerable = bool(question["answerable"])
        raw_results = result_map.get(question_id, ())
        if isinstance(raw_results, BaseException):
            results.append(
                QuestionResult(
                    question_id=question_id,
                    group_id=str(question["group_id"]),
                    category=str(question["category"]),
                    split=str(question["split"]),
                    answerable=answerable,
                    relevant_spans=spans,
                    hit_relevant_spans=0,
                    status="error",
                    error_code=type(raw_results).__name__,
                )
            )
            continue

        retrieved = tuple(raw_results)
        recalls = {
            k: (
                sum(
                    any(_span_is_covered(result, span) for result in retrieved[:k])
                    for span in spans
                )
                / len(spans)
                if answerable and spans
                else None
            )
            for k in (1, 3, 5)
        }
        relevant_count_at_5 = sum(
            any(_span_is_covered(result, span) for result in retrieved[:5])
            for span in spans
        )
        relevant_count_at_5 = relevant_count_at_5 if answerable else 0
        ranks = tuple(range(1, len(retrieved) + 1))
        first_rank = next(
            (
                rank
                for rank, result in enumerate(retrieved[:5], 1)
                if any(_span_is_covered(result, span) for span in spans)
            ),
            None,
        )
        results.append(
            QuestionResult(
                question_id=question_id,
                group_id=str(question["group_id"]),
                category=str(question["category"]),
                split=str(question["split"]),
                answerable=answerable,
                relevant_spans=spans,
                retrieved_chunk_ids=tuple(item.chunk_id for item in retrieved),
                retrieved_ranks=tuple(ranks),
                retrieved_scores=tuple(item.score for item in retrieved),
                score_type=score_type,
                hit_relevant_spans=relevant_count_at_5,
                recall_at_1=recalls[1],
                recall_at_3=recalls[3],
                recall_at_5=recalls[5],
                recall=recalls[5],
                reciprocal_rank=1.0 / first_rank if first_rank else (0.0 if answerable else None),
                hit=bool(first_rank) if answerable else None,
                status="complete",
            )
        )

    summary = _summarize(results)
    by_category = {
        category: _summarize([item for item in results if item.category == category])
        for category in sorted({item.category for item in results})
    }
    return tuple(results), EvaluationSummary(
        question_count=len(results),
        answerable_count=sum(item.answerable for item in results),
        unanswerable_count=sum(not item.answerable for item in results),
        error_count=sum(item.status == "error" for item in results),
        by_category=by_category,
        **summary,
    )