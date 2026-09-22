import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.knowledge.evaluation import (
    EvaluationQuestion,
    build_parser,
    evaluate_retrieval,
    evaluate_question_results,
    load_ablation_configs,
    mean_reciprocal_rank,
    recall_at_k,
    summarize_question_results,
    validate_min_vector_similarity,
)
from app.knowledge.ingestion import ingest_manifest
from app.knowledge.models import SearchResult
from app.knowledge.repository import KnowledgeRepository


def make_result(
    chunk_id: str,
    document_id: str,
    version: str,
    start_line: int,
    end_line: int,
) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        document_id=document_id,
        document_version=version,
        source_path=f"{document_id}.md",
        source_url=None,
        title=document_id,
        heading_path="章节",
        start_line=start_line,
        end_line=end_line,
        text=f"正文 {chunk_id}",
        score=1.0,
    )


def test_recall_at_k_counts_relevant_spans_by_version_and_line_coverage():
    results = [
        make_result("wrong-version", "redis", "v0", 1, 20),
        make_result("hit-first", "redis", "v1", 5, 8),
        make_result("partial", "redis", "v1", 9, 10),
    ]
    spans = [
        {"document_id": "redis", "document_version": "v1", "start_line": 5, "end_line": 8},
        {"document_id": "redis", "document_version": "v1", "start_line": 9, "end_line": 11},
    ]

    assert recall_at_k(results, spans, k=5) == pytest.approx(0.5)


def test_mrr_at_k_is_reciprocal_rank_of_first_fully_covered_span():
    results = [
        make_result("unrelated", "cache", "v1", 1, 3),
        make_result("relevant", "redis", "v1", 5, 8),
    ]
    spans = [
        {"document_id": "redis", "document_version": "v1", "start_line": 5, "end_line": 8}
    ]

    assert mean_reciprocal_rank(results, spans, k=5) == pytest.approx(0.5)
    assert mean_reciprocal_rank([], spans, k=5) == 0.0


def test_evaluate_retrieval_separates_unanswerable_questions_and_refusals():
    questions = [
        EvaluationQuestion(
            question_id="answerable",
            query="SETEX",
            category="术语",
            split="dev",
            answerable=True,
            relevant_spans=(
                {"document_id": "redis", "document_version": "v1", "start_line": 5, "end_line": 8},
            ),
            reference_answer="设置过期时间",
        ),
        EvaluationQuestion(
            question_id="unanswerable",
            query="数据库分片如何扩容？",
            category="无答案",
            split="dev",
            answerable=False,
            relevant_spans=(),
            reference_answer="材料不足",
        ),
    ]
    result_map = {
        "answerable": [make_result("hit", "redis", "v1", 5, 8)],
        "unanswerable": [],
    }

    summary = evaluate_retrieval(questions, result_map, k=5)

    assert summary.question_count == 2
    assert summary.answerable_count == 1
    assert summary.recall_at_k == pytest.approx(1.0)
    assert summary.mrr_at_k == pytest.approx(1.0)
    assert summary.unanswerable_count == 1
    assert summary.correct_refusal_count == 1
    assert summary.incorrect_answer_count == 0


def test_question_results_record_hits_statuses_and_execution_errors():
    questions = [
        EvaluationQuestion(
            question_id="hit",
            query="SETEX",
            category="术语",
            split="dev",
            answerable=True,
            relevant_spans=(
                {"document_id": "redis", "document_version": "v1", "start_line": 5, "end_line": 8},
            ),
            reference_answer="设置过期时间",
        ),
        EvaluationQuestion(
            question_id="empty-answerable",
            query="缓存淘汰",
            category="改写",
            split="dev",
            answerable=True,
            relevant_spans=(
                {"document_id": "cache", "document_version": "v1", "start_line": 2, "end_line": 4},
            ),
            reference_answer="材料中的缓存淘汰说明",
        ),
        EvaluationQuestion(
            question_id="empty-unanswerable",
            query="数据库分片如何扩容？",
            category="无答案",
            split="dev",
            answerable=False,
            relevant_spans=(),
            reference_answer="材料不足",
        ),
        EvaluationQuestion(
            question_id="wrong-unanswerable",
            query="Kafka 集群升级",
            category="无答案",
            split="dev",
            answerable=False,
            relevant_spans=(),
            reference_answer="材料不足",
        ),
        EvaluationQuestion(
            question_id="failed",
            query="请求失败",
            category="术语",
            split="dev",
            answerable=True,
            relevant_spans=(
                {"document_id": "redis", "document_version": "v1", "start_line": 5, "end_line": 8},
            ),
            reference_answer="不会计入质量指标",
        ),
    ]
    result_map = {
        "hit": [make_result("hit-chunk", "redis", "v1", 5, 8)],
        "empty-answerable": [],
        "empty-unanswerable": [],
        "wrong-unanswerable": [make_result("wrong-chunk", "cache", "v1", 1, 2)],
        "failed": [],
    }

    results = evaluate_question_results(
        questions,
        result_map,
        error_map={"failed": "provider timeout"},
        score_type="cosine",
        latency_map={question.question_id: 1.5 for question in questions},
        k=5,
    )

    by_id = {result.question_id: result for result in results}
    assert by_id["hit"].status == "success"
    assert by_id["hit"].hit_at_k is True
    assert by_id["hit"].retrieved_results[0]["rank"] == 1
    assert by_id["hit"].retrieved_results[0]["score_type"] == "cosine"
    assert by_id["hit"].retrieved_results[0]["covered_span_indexes"] == [0]
    assert by_id["empty-answerable"].status == "empty"
    assert by_id["empty-unanswerable"].status == "empty"
    assert by_id["wrong-unanswerable"].status == "success"
    assert by_id["failed"].status == "error"
    assert by_id["failed"].error == "provider timeout"
    assert by_id["failed"].recall_at_k is None

    summary = summarize_question_results(results, k=5)
    assert summary.evaluated_answerable_count == 2
    assert summary.recall_at_k == pytest.approx(0.5)
    assert summary.hit_at_k == pytest.approx(0.5)
    assert summary.answerable_empty_result_count == 1
    assert summary.correct_refusal_count == 1
    assert summary.incorrect_answer_count == 1
    assert summary.execution_error_count == 1
    assert summary.evaluated_unanswerable_count == 2


def test_question_results_record_candidate_and_final_evidence_metrics_without_cross_score_comparison():
    question = EvaluationQuestion(
        question_id="multi",
        query="Q/K/V 以及多头注意力",
        category="跨文章",
        split="dev",
        answerable=True,
        relevant_spans=(
            {"document_id": "doc-a", "document_version": "v1", "start_line": 1, "end_line": 2},
            {"document_id": "doc-b", "document_version": "v1", "start_line": 3, "end_line": 4},
        ),
        reference_answer="需要两组证据",
    )
    candidate = make_result("candidate", "doc-a", "v1", 1, 2)
    final = make_result("final", "doc-b", "v1", 3, 4)

    results = evaluate_question_results(
        [question],
        {"multi": [final]},
        candidate_result_map={"multi": [candidate]},
        final_result_map={"multi": [final]},
        score_type="cosine",
        candidate_score_type="bm25",
        final_score_type="rerank",
        evidence_status_map={"multi": "supported"},
        phase_latency_map={
            "multi": {
                "query_planner": 0.5,
                "candidate": 1.0,
                "rerank": 2.0,
                "selection": 3.0,
                "judge": 4.0,
            }
        },
        source_coverage_map={"multi": {"covered": 2, "required": 2}},
        judge_reason_map={"multi": "coverage_complete"},
        judge_executed_map={"multi": True},
        k=5,
    )

    item = results[0]
    assert item.candidate_evidence_recall_at_k == pytest.approx(0.5)
    assert item.final_evidence_recall == pytest.approx(0.5)
    assert item.source_coverage == {"covered": 2, "required": 2}
    assert item.evidence_status == "supported"
    assert item.phase_latency_ms == {
        "query_planner": 0.5,
        "candidate": 1.0,
        "rerank": 2.0,
        "selection": 3.0,
        "judge": 4.0,
    }
    assert item.judge_reason == "coverage_complete"
    assert item.judge_executed is True
    assert item.candidate_results[0]["score_type"] == "bm25"
    assert item.final_results[0]["score_type"] == "rerank"


def test_evaluation_parser_maps_legacy_top_k_to_final_limit_and_accepts_ablation_fields():
    args = build_parser().parse_args(
        [
            "--dataset", "questions.jsonl",
            "--split", "dev",
            "--mode", "hybrid",
            "--output", "report.json",
            "--top-k", "5",
            "--candidate-limit", "30",
            "--candidate-min-vector-similarity", "0.2",
            "--query-planning", "conditional",
            "--rerank", "noop",
            "--evidence-selection", "coverage-aware",
            "--answerability", "coverage-v1",
        ]
    )

    assert args.top_k == 5
    assert args.final_limit == 5
    assert args.candidate_limit == 30
    assert args.candidate_min_vector_similarity == pytest.approx(0.2)
    assert args.query_planning == "conditional"
    assert args.rerank == "noop"


def test_evaluation_parser_maps_top_k_to_both_limits_by_default():
    args = build_parser().parse_args(
        [
            "--dataset", "questions.jsonl",
            "--split", "dev",
            "--mode", "keyword",
            "--output", "report.json",
        ]
    )

    assert args.top_k == 5
    assert args.final_limit == 5
    assert args.candidate_limit == 5


def test_question_results_can_be_summarized_by_category_and_answerability():
    questions = [
        EvaluationQuestion(
            question_id="one",
            query="one",
            category="术语",
            split="dev",
            answerable=True,
            relevant_spans=(
                {"document_id": "redis", "document_version": "v1", "start_line": 1, "end_line": 1},
            ),
            reference_answer="one",
        ),
        EvaluationQuestion(
            question_id="two",
            query="two",
            category="无答案",
            split="dev",
            answerable=False,
            relevant_spans=(),
            reference_answer="拒答",
        ),
    ]
    results = evaluate_question_results(
        questions,
        {"one": [make_result("one-chunk", "redis", "v1", 1, 1)], "two": []},
        score_type="bm25",
        k=5,
    )

    summary = summarize_question_results(results, k=5, group_by="category")

    assert summary["术语"].question_count == 1
    assert summary["术语"].recall_at_k == pytest.approx(1.0)
    assert summary["无答案"].correct_refusal_count == 1


@pytest.mark.parametrize("threshold", [-0.01, 1.01, float("nan"), float("inf")])
def test_evaluation_rejects_invalid_min_vector_similarity(threshold: float):
    with pytest.raises(ValueError, match="similarity"):
        validate_min_vector_similarity(threshold)


def test_evaluation_cli_parses_min_vector_similarity_from_command_line():
    args = build_parser().parse_args(
        [
            "--dataset",
            "questions.jsonl",
            "--split",
            "dev",
            "--mode",
            "vector",
            "--output",
            "report.json",
            "--min-vector-similarity",
            "0.5",
        ]
    )

    assert args.min_vector_similarity == pytest.approx(0.5)


def test_evaluation_cli_writes_reproducible_keyword_report(tmp_path: Path):
    fixture_root = Path(__file__).parent / "fixtures" / "knowledge"
    database = tmp_path / "knowledge.db"
    repository = KnowledgeRepository(database)
    import asyncio

    asyncio.run(repository.init())
    asyncio.run(
        ingest_manifest(repository, fixture_root / "manifest.json", fixture_root)
    )
    document = asyncio.run(repository.get_document_by_path("redis.md"))
    assert document is not None
    dataset = tmp_path / "questions.jsonl"
    dataset.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "setex",
                        "query": "SETEX 过期时间",
                        "category": "术语",
                        "split": "dev",
                        "answerable": True,
                        "relevant_spans": [
                            {
                                "document_id": document.document_id,
                                "document_version": document.document_version,
                                "start_line": 5,
                                "end_line": 7,
                            }
                        ],
                        "reference_answer": "SETEX 同时设置值和过期时间",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "report.json"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.knowledge.evaluation",
            "--dataset",
            str(dataset),
            "--split",
            "dev",
            "--mode",
            "keyword",
            "--output",
            str(output),
            "--database",
            str(database),
        ],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["mode"] == "keyword"
    assert report["split"] == "dev"
    assert report["question_count"] == 1
    assert "recall_at_5" in report
    assert report["retrieval_latency_ms"]["mean"] >= 0
    assert report["source_coverage"] == {"covered": 1, "required": 1}
    assert set(report["phase_latency_ms"]) == {
        "query_planner", "candidate", "rerank", "selection", "judge", "llm"
    }
    assert report["execution"] == {
        "query_planner_executed": False,
        "judge_executed": False,
        "llm_executed": False,
        "answer_quality_evaluated": False,
    }
    assert report["blog_formal_014"] is None
    question_result = report["question_results"][0]
    assert question_result["judge_executed"] is False
    assert question_result["judge_reason"] is None
    assert question_result["llm_called"] is False
    assert question_result["citation_complete"] is None