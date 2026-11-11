import json
from pathlib import Path

import pytest

from app.memory.evaluation import run_memory_evaluation


DATASET = Path(__file__).parents[1] / "evals" / "memory_retrieval_v1.json"
ACCEPTANCE_DATASET = Path(__file__).parents[1] / "evals" / "memory_retrieval_v2.json"


@pytest.mark.asyncio
async def test_fixed_memory_retrieval_dataset_reports_quality_and_scope_metrics(tmp_path):
    report = await run_memory_evaluation(DATASET, output_path=tmp_path / "report.json")

    assert report["dataset_id"] == "personal-assistant-memory-retrieval-v1"
    assert len(report["dataset_sha256"]) == 64
    assert report["retrieval_mode"] == "keyword"
    assert report["summary"]["case_count"] == 16
    assert report["summary"]["answerable_case_count"] == 13
    assert report["summary"]["recall_at_8"] >= 0.90
    assert report["summary"]["mrr"] >= 0.90
    assert report["summary"]["empty_result_accuracy"] == 1.0
    assert report["summary"]["scope_leak_count"] == 0
    assert report["summary"]["over_budget_count"] == 0

    cases = {item["case_id"]: item for item in report["cases"]}
    assert "background-running" not in cases["semantic-gap"]["retrieved_ids"]
    assert "project-api" in cases["project-runtime"]["expected_ids"]
    assert "project-api" in cases["project-runtime"]["retrieved_ids"]
    assert "habit-planning" in cases["planning-habit"]["retrieved_ids"]
    assert "background-accessibility" in cases["accessibility-background"]["retrieved_ids"]
    assert cases["unanswerable-finance"]["empty_result_correct"] is True
    assert "forgotten-finance" not in cases["unanswerable-finance"]["retrieved_ids"]
    assert cases["semantic-gap"]["recall"] == 0.0

    saved = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert saved["dataset_sha256"] == report["dataset_sha256"]


@pytest.mark.asyncio
async def test_memory_evaluation_rejects_output_overwriting_its_dataset(tmp_path):
    with pytest.raises(ValueError, match="must not overwrite"):
        await run_memory_evaluation(DATASET, output_path=DATASET)


@pytest.mark.asyncio
async def test_memory_acceptance_dataset_separates_global_preferences_from_query_recall():
    report = await run_memory_evaluation(ACCEPTANCE_DATASET)

    assert report["dataset_id"] == "personal-assistant-memory-retrieval-v2"
    assert len(report["dataset_sha256"]) == 64
    assert report["summary"]["case_count"] == 45
    assert report["summary"]["query_fact_answerable_case_count"] == 27
    assert report["summary"]["recall_at_8"] >= 0.95
    assert report["summary"]["query_fact_recall_at_8"] >= 0.95
    assert report["summary"]["query_fact_mrr"] >= 0.95
    assert report["summary"]["empty_result_accuracy"] == 1.0
    assert report["summary"]["scope_leak_count"] == 0
    assert report["summary"]["over_budget_count"] == 0

    cases = {item["case_id"]: item for item in report["cases"]}
    assert cases["semantic-gap"]["query_fact_recall"] == 0.0
    assert cases["unanswerable-scope"]["empty_result_correct"] is True
    assert "pref-language-old" not in cases["current-language"]["retrieved_ids"]
    assert cases["temporary-english"]["empty_result_correct"] is True
    assert cases["superseded-english"]["empty_result_correct"] is True


@pytest.mark.asyncio
async def test_hybrid_memory_evaluation_reports_semantic_recall_and_provider_usage():
    class SemanticEmbeddingClient:
        dimensions = 2

        def __init__(self):
            self.calls = 0

        async def embed(self, texts):
            self.calls += 1
            return [[1.0, 0.0] if "有氧锻炼" in text or "慢跑训练" in text
                    else [0.0, 1.0] for text in texts]

    client = SemanticEmbeddingClient()
    report = await run_memory_evaluation(
        ACCEPTANCE_DATASET, embedding_client=client, embedding_model="fake-unit|2",
    )

    assert report["retrieval_mode"] == "hybrid"
    assert report["min_vector_similarity"] == 0.5
    assert report["summary"]["hybrid_case_count"] == 45
    assert report["summary"]["keyword_fallback_case_count"] == 0
    assert report["provider_request_count"] is None
    semantic_gap = next(case for case in report["cases"] if case["case_id"] == "semantic-gap")
    assert semantic_gap["query_fact_recall"] == 1.0
    assert semantic_gap["retrieval_mode"] == "hybrid"
    assert "background-running" in semantic_gap["retrieved_ids"]
    assert "background-running" in semantic_gap["vector_similarities"]
    dataset_ids = {fact["id"] for fact in json.loads(
        ACCEPTANCE_DATASET.read_text(encoding="utf-8"),
    )["facts"]}
    assert set(semantic_gap["vector_similarities"]) <= dataset_ids


@pytest.mark.asyncio
async def test_evaluation_embedding_budget_stops_before_request_limit_is_exceeded():
    from app.memory.evaluation import EvaluationEmbeddingBudget

    class CountingClient:
        dimensions = 2

        def __init__(self):
            self.calls = 0

        async def embed(self, texts):
            self.calls += 1
            return [[1.0, 0.0] for _ in texts]

    client = CountingClient()
    budget = EvaluationEmbeddingBudget(client, request_limit=1)
    await budget.embed(["first"])

    with pytest.raises(RuntimeError, match="memory_evaluation_provider_request_limit"):
        await budget.embed(["second"])
    assert client.calls == 1
    assert budget.request_count == 1
    assert budget.input_count == 1
    assert budget.budget_exceeded
