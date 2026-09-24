import math

import pytest
from pydantic import ValidationError

from app.knowledge.benchmark_models import (
    BenchmarkManifest,
    ComparisonReport,
    QuestionResult,
    RunReport,
    StrategyConfig,
)


def make_strategy(**overrides):
    values = {
        "name": "hybrid-v1",
        "mode": "hybrid",
        "top_k": 5,
        "candidate_limit": 10,
        "final_limit": 5,
        "similarity_threshold": 0.5,
    }
    values.update(overrides)
    return values


def make_question(**overrides):
    values = {
        "question_id": "q-1",
        "group_id": "group-1",
        "category": "术语",
        "split": "dev",
        "answerable": True,
        "relevant_spans": [
            {
                "document_id": "doc-1",
                "document_version": "version-1",
                "start_line": 1,
                "end_line": 3,
            }
        ],
        "retrieved_chunk_ids": ["chunk-1"],
        "retrieved_ranks": [1],
        "hit_relevant_spans": 1,
        "recall": 1.0,
        "reciprocal_rank": 1.0,
        "hit": True,
        "status": "complete",
        "duration_ms": 3.5,
    }
    values.update(overrides)
    return values


def make_manifest(**overrides):
    values = {
        "schema_version": "benchmark-manifest/v1",
        "dataset": "blog_questions.jsonl",
        "split": "dev",
        "database": "/data/rag/databases/snapshot.db",
        "comparison_scope": "fixed_index",
        "allowed_changes": ["retrieval_mode", "similarity_threshold"],
        "strategy": make_strategy(),
    }
    values.update(overrides)
    return values


def test_strategy_rejects_unknown_fields_and_invalid_ranges():
    with pytest.raises(ValidationError):
        StrategyConfig(**make_strategy(unknown_setting=True))

    with pytest.raises(ValidationError):
        StrategyConfig(**make_strategy(top_k=0))

    with pytest.raises(ValidationError):
        StrategyConfig(**make_strategy(similarity_threshold=1.1))

    with pytest.raises(ValidationError, match="unknown strategy parameters"):
        StrategyConfig(**make_strategy(parameters={"unapproved": "value"}))


def test_manifest_rejects_duplicate_question_ids_and_group_leakage():
    with pytest.raises(ValidationError, match="Duplicate question_id"):
        BenchmarkManifest(
            **make_manifest(
                question_ids=["q-1", "q-1"],
                question_groups={"q-1": "group-1"},
            )
        )

    with pytest.raises(ValidationError, match="group_id"):
        BenchmarkManifest(
            **make_manifest(
                question_ids=["q-dev", "q-test"],
                question_groups={"q-dev": "shared", "q-test": "shared"},
                question_splits={"q-dev": "dev", "q-test": "test"},
                split="dev",
            )
        )


def test_manifest_rejects_empty_split_and_unknown_configuration():
    with pytest.raises(ValidationError, match="split"):
        BenchmarkManifest(**make_manifest(split=""))

    with pytest.raises(ValidationError):
        BenchmarkManifest(**make_manifest(extra_option=True))


def test_unanswerable_question_requires_empty_evidence():
    with pytest.raises(ValidationError, match="unanswerable"):
        QuestionResult(
            **make_question(
                answerable=False,
                relevant_spans=[{
                    "document_id": "doc-1",
                    "document_version": "version-1",
                    "start_line": 1,
                    "end_line": 3,
                }],
            )
        )


def test_question_result_rejects_non_finite_scores_and_invalid_evidence():
    with pytest.raises(ValidationError):
        QuestionResult(**make_question(recall=math.nan))

    with pytest.raises(ValidationError, match="line"):
        QuestionResult(
            **make_question(
                relevant_spans=[{
                    "document_id": "doc-1",
                    "document_version": "version-1",
                    "start_line": 4,
                    "end_line": 2,
                }]
            )
        )


def test_run_report_requires_complete_question_coverage():
    question = QuestionResult(**make_question())
    with pytest.raises(ValidationError, match="question"):
        RunReport(
            schema_version="benchmark-run/v1",
            run_id="run-1",
            manifest=BenchmarkManifest(**make_manifest()),
            strategy=StrategyConfig(**make_strategy()),
            dataset_sha256="a" * 64,
            index_sha256="b" * 64,
            source_sha256="c" * 64,
            metric_version="retrieval/v2",
            status="complete",
            questions=[question],
            expected_question_ids=["q-1", "q-2"],
            summary={"question_count": 1},
        )


def test_comparison_report_requires_reason_for_incompatible_runs():
    with pytest.raises(ValidationError, match="reason"):
        ComparisonReport(
            schema_version="benchmark-comparison/v1",
            baseline_run_id="baseline",
            candidate_run_id="candidate",
            status="incomparable",
            reason="",
            metric_deltas={},
            question_deltas=[],
        )