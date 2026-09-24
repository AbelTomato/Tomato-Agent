from __future__ import annotations

import json

import pytest

from app.knowledge.benchmark_compare import compare_reports, write_comparison
from app.knowledge.benchmark_models import BenchmarkManifest, RunReport, StrategyConfig


def strategy(name: str = "keyword-v1") -> StrategyConfig:
    return StrategyConfig(
        name=name,
        mode="keyword",
        top_k=5,
        candidate_limit=10,
        final_limit=5,
        similarity_threshold=0.5,
    )


def manifest(scope: str = "fixed_index", allowed_changes: tuple[str, ...] = ()) -> BenchmarkManifest:
    return BenchmarkManifest(
        schema_version="benchmark-manifest/v1",
        dataset="questions.jsonl",
        split="dev",
        database="snapshot.db",
        comparison_scope=scope,
        allowed_changes=allowed_changes,
        strategy=strategy(),
        question_ids=("q1", "q2"),
        question_groups={"q1": "g1", "q2": "g2"},
        question_splits={"q1": "dev", "q2": "dev"},
    )


def report(
    run_id: str,
    *,
    recall: float,
    index_sha256: str = "index-a",
    status: str = "complete",
    metric_version: str = "benchmark-metrics/v1",
    questions: list[dict] | None = None,
) -> RunReport:
    items = questions if questions is not None else [
        {
            "question_id": "q1", "group_id": "g1", "category": "term", "split": "dev",
            "answerable": True, "relevant_spans": [], "hit_relevant_spans": 0,
            "recall": recall, "status": "complete",
        },
        {
            "question_id": "q2", "group_id": "g2", "category": "term", "split": "dev",
            "answerable": False, "relevant_spans": [], "hit_relevant_spans": 0,
            "status": "complete",
        },
    ]
    return RunReport(
        schema_version="benchmark-run/v1",
        run_id=run_id,
        manifest=manifest(),
        strategy=strategy(run_id),
        dataset_sha256="dataset-a",
        corpus_sha256="corpus-a",
        index_sha256=index_sha256,
        source_sha256="source-a",
        metric_version=metric_version,
        status=status,
        questions=tuple(items),
        expected_question_ids=tuple(item["question_id"] for item in items),
        summary={"recall_at_5": {"value": recall, "sample_count": 1}},
    )


def test_comparison_pairs_questions_and_zero_baseline_relative_delta_is_null():
    comparison = compare_reports(report("baseline", recall=0.0), report("candidate", recall=0.5))

    assert comparison.status == "improved"
    assert comparison.metric_deltas["recall_at_5"].absolute == 0.5
    assert comparison.metric_deltas["recall_at_5"].relative is None
    assert [item.question_id for item in comparison.question_deltas] == ["q1", "q2"]
    assert comparison.question_deltas[0].classification == "improved"


def test_comparison_rejects_missing_question_pair():
    baseline = report("baseline", recall=0.5)
    candidate = report("candidate", recall=0.5, questions=[
        {
            "question_id": "q1", "group_id": "g1", "category": "term", "split": "dev",
            "answerable": True, "relevant_spans": [], "hit_relevant_spans": 0,
            "recall": 0.5, "status": "complete",
        }
    ])
    with pytest.raises(ValueError, match="question IDs"):
        compare_reports(baseline, candidate)


def test_comparison_marks_failed_runs_incomplete():
    comparison = compare_reports(
        report("baseline", recall=0.5, status="incomplete"),
        report("candidate", recall=0.7),
    )
    assert comparison.status == "incomplete"


def test_comparison_marks_metric_version_conflict_incomparable():
    comparison = compare_reports(
        report("baseline", recall=0.5),
        report("candidate", recall=0.7, metric_version="benchmark-metrics/v2"),
    )
    assert comparison.status == "incomparable"
    assert "metric" in comparison.reason


def test_comparison_marks_undeclared_index_change_incomparable():
    baseline = report("baseline", recall=0.5)
    candidate = report("candidate", recall=0.7, index_sha256="index-b")
    comparison = compare_reports(baseline, candidate)
    assert comparison.status == "incomparable"
    assert "index" in comparison.reason


def test_index_strategy_change_requires_declaration_on_both_runs():
    baseline = report("baseline", recall=0.5, index_sha256="index-a")
    candidate = report("candidate", recall=0.7, index_sha256="index-b")
    baseline = baseline.model_copy(update={"manifest": manifest("index_strategy", ("index",))})
    candidate = candidate.model_copy(update={"manifest": manifest("index_strategy", ("index",))})

    assert compare_reports(baseline, candidate).status == "improved"


def test_comparison_writer_emits_consistent_json_markdown_and_refuses_overwrite(tmp_path):
    comparison = compare_reports(report("baseline", recall=0.5), report("candidate", recall=0.7))
    json_path, markdown_path = write_comparison(comparison, tmp_path / "comparison")

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["status"] == comparison.status
    assert "0.2" in markdown_path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_comparison(comparison, tmp_path / "comparison")