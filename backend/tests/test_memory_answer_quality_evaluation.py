import hashlib
import json
from pathlib import Path

import pytest

from app.agent.models import LLMResponse
from app.memory.answer_quality_evaluation import (
    CONDITIONS,
    AnswerQualityDataset,
    render_blind_review_worksheet,
    run_memory_answer_quality_evaluation,
)
from app.memory.extraction_quality_evaluation import EvaluationLLMBudget


DATASET = Path(__file__).parents[1] / "evals" / "memory_answer_quality_v1.json"


class FakeLLM:
    def __init__(self):
        self.calls = 0
        self.last_token_usage = None

    async def complete(self, messages, tools):
        self.calls += 1
        self.last_token_usage = {"prompt_tokens": 3, "completion_tokens": 2}
        return LLMResponse(kind="final", content=f"fake answer {self.calls}")


def test_answer_quality_dataset_is_valid_and_covers_expected_categories():
    dataset = AnswerQualityDataset.model_validate_json(DATASET.read_bytes())

    assert len(dataset.cases) == 45
    assert len({case.id for case in dataset.cases}) == 45
    assert any(case.recent_history for case in dataset.cases)
    assert any(not case.fact_ids for case in dataset.cases)


@pytest.mark.asyncio
async def test_answer_quality_report_is_blinded_and_key_is_hash_bound(tmp_path):
    client = FakeLLM()
    budget = EvaluationLLMBudget(client, request_limit=45 * len(CONDITIONS))
    report_path = tmp_path / "report.json"
    key_path = tmp_path / "condition-key.json"

    report = await run_memory_answer_quality_evaluation(
        DATASET, budget, model_label="fake", request_limit=budget.request_limit,
        random_seed=31, output_path=report_path, condition_key_path=key_path,
    )

    serialized = report_path.read_text(encoding="utf-8")
    key = json.loads(key_path.read_text(encoding="utf-8"))
    assert report["status"] == "complete"
    assert report["case_count"] == 45
    assert report["request_count"] == client.calls == 180
    assert report["condition_count"] == 4
    assert "conditions" not in report
    assert all(condition not in serialized for condition in CONDITIONS)
    assert len(key["mapping"]) == 180
    assert {entry["condition"] for entry in key["mapping"].values()} == set(CONDITIONS)
    assert key["report_sha256"] == hashlib.sha256(report_path.read_bytes()).hexdigest()

    worksheet = render_blind_review_worksheet(report_path, output_path=tmp_path / "review.md")
    assert worksheet.count("### 匿名答案") == 180
    assert all(condition not in worksheet for condition in CONDITIONS)
    assert "fake answer" in worksheet


@pytest.mark.asyncio
async def test_answer_quality_rejects_insufficient_budget_before_provider_call():
    client = FakeLLM()
    budget = EvaluationLLMBudget(client, request_limit=179)

    with pytest.raises(ValueError, match="must cover all 180"):
        await run_memory_answer_quality_evaluation(
            DATASET, budget, model_label="fake", request_limit=179, random_seed=1,
        )

    assert client.calls == 0
    assert budget.request_count == 0


@pytest.mark.asyncio
async def test_answer_quality_marks_incomplete_when_budget_was_previously_consumed(tmp_path):
    client = FakeLLM()
    budget = EvaluationLLMBudget(client, request_limit=180)
    budget.request_count = 180

    report = await run_memory_answer_quality_evaluation(
        DATASET, budget, model_label="fake", request_limit=180, random_seed=1,
        output_path=tmp_path / "report.json",
    )

    assert report["status"] == "incomplete"
    assert report["request_count"] == 180
    assert report["blocked_attempt_count"] == 1
    assert client.calls == 0
    assert report["case_count"] == 1
