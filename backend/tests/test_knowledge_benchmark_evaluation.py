import pytest

from app.knowledge.benchmark_evaluation import evaluate_questions
from app.knowledge.models import SearchResult


def make_result(chunk_id: str, start: int, end: int) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        document_id="doc",
        document_version="v1",
        source_path="doc.md",
        source_url=None,
        title="Doc",
        heading_path="Section",
        start_line=start,
        end_line=end,
        text="content",
        score=1.0,
    )


def test_evaluate_questions_computes_evidence_metrics_and_empty_rates():
    questions = [
        {
            "id": "q1",
            "group_id": "g1",
            "category": "api",
            "split": "dev",
            "answerable": True,
            "relevant_spans": [
                {
                    "document_id": "doc",
                    "document_version": "v1",
                    "start_line": 1,
                    "end_line": 2,
                },
                {
                    "document_id": "doc",
                    "document_version": "v1",
                    "start_line": 5,
                    "end_line": 6,
                },
            ],
        },
        {
            "id": "q2",
            "group_id": "g2",
            "category": "api",
            "split": "dev",
            "answerable": True,
            "relevant_spans": [
                {
                    "document_id": "doc",
                    "document_version": "v1",
                    "start_line": 10,
                    "end_line": 11,
                }
            ],
        },
        {
            "id": "q3",
            "group_id": "g3",
            "category": "no_answer",
            "split": "dev",
            "answerable": False,
            "relevant_spans": [],
        },
    ]
    result, summary = evaluate_questions(
        questions,
        {"q1": [make_result("hit-second", 1, 2), make_result("miss", 3, 4)]},
        score_type="bm25",
    )

    q1 = next(item for item in result if item.question_id == "q1")
    assert q1.recall == pytest.approx(0.5)
    assert q1.reciprocal_rank == pytest.approx(1.0)
    assert q1.hit is True
    assert q1.retrieved_ranks == (1, 2)
    assert q1.score_type == "bm25"
    assert summary.recall_at_1.value == pytest.approx(0.25)
    assert summary.recall_at_1.sample_count == 2
    assert summary.recall_at_3.value == pytest.approx(0.25)
    assert summary.mrr_at_5.value == pytest.approx(0.5)
    assert summary.hit_at_5.value == pytest.approx(0.5)
    assert summary.answerable_empty_result_rate.value == pytest.approx(0.5)
    assert summary.unanswerable_empty_result_rate.value == 1.0
    assert summary.unanswerable_nonempty_rate.value == 0.0
    assert summary.by_category["api"]["recall_at_1"].sample_count == 2


def test_errors_do_not_count_as_empty_or_zero_quality_scores():
    questions = [
        {
            "id": "q1",
            "group_id": "g1",
            "category": "api",
            "split": "dev",
            "answerable": True,
            "relevant_spans": [
                {
                    "document_id": "doc",
                    "document_version": "v1",
                    "start_line": 1,
                    "end_line": 2,
                }
            ],
        },
        {
            "id": "q2",
            "group_id": "g2",
            "category": "other",
            "split": "dev",
            "answerable": False,
            "relevant_spans": [],
        },
    ]
    results, summary = evaluate_questions(
        questions,
        {"q1": RuntimeError("timeout"), "q2": []},
        score_type="bm25",
    )

    assert next(item for item in results if item.question_id == "q1").status == "error"
    assert summary.error_count == 1
    assert summary.execution_error_rate.value == pytest.approx(0.5)
    assert summary.answerable_empty_result_rate.value is None
    assert summary.answerable_empty_result_rate.sample_count == 0
    assert summary.recall_at_5.value is None
    assert summary.recall_at_5.sample_count == 0
    assert summary.unanswerable_empty_result_rate.value == 1.0