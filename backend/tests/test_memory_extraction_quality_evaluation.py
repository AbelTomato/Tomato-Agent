import json
from pathlib import Path

import pytest

from app.agent.models import LLMResponse
from app.memory.extraction import MemoryExtractor
from app.memory.extraction_quality_evaluation import (
    EvaluationLLMBudget,
    run_extraction_quality_evaluation,
    score_saved_extraction_report,
)


DATASET = Path(__file__).parents[1] / "evals" / "memory_extraction_quality_v1.json"
HOLDOUT_DATASET = Path(__file__).parents[1] / "evals" / "memory_extraction_quality_holdout_v1.json"
HOLDOUT_GOLD = Path(__file__).parents[1] / "evals" / "memory_extraction_quality_holdout_gold_v1.json"


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.last_token_usage = {"prompt_tokens": 10, "completion_tokens": 4}

    async def complete(self, messages, tools):
        self.calls += 1
        self.last_token_usage = {"prompt_tokens": 10, "completion_tokens": 4}
        response = self.responses.pop(0)
        return LLMResponse(kind="final", content=json.dumps(response, ensure_ascii=False))


def fact(*, message_id="u1", content="偏好简体中文回答", kind="preference",
         topic_key="response.language"):
    return {"facts": [{
        "kind": kind,
        "topic_key": topic_key,
        "content": content,
        "evidence_message_ids": [message_id],
        "confidence": 0.99,
        "self_reported": True,
        "durable": True,
    }]}


@pytest.mark.asyncio
async def test_extraction_quality_evaluation_matches_gold_and_reports_hardening(tmp_path):
    dataset = {
        "schema_version": "memory-extraction-quality-dataset/v1",
        "dataset_id": "unit-extraction-quality",
        "cases": [{
            "id": "positive",
            "messages": [{"message_id": "u1", "role": "user", "content": "我偏好简体中文回答"}],
            "expected_facts": [{
                "kind": "preference", "topic_prefixes": ["response."],
                "evidence_message_ids": ["u1"], "content_any": ["中文"],
            }],
        }, {
            "id": "negative",
            "messages": [{"message_id": "u2", "role": "user", "content": "这次用英文"}],
            "expected_facts": [],
        }],
    }
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(dataset, ensure_ascii=False), encoding="utf-8")
    llm = ScriptedLLM([fact(), {"facts": []}])
    budget = EvaluationLLMBudget(llm, request_limit=2)
    report = await run_extraction_quality_evaluation(
        dataset_path, MemoryExtractor(budget), dataset_label="fixture", request_limit=2,
        output_path=tmp_path / "report.json",
    )

    assert report["status"] == "complete"
    assert report["summary"]["true_positive"] == 1
    assert report["summary"]["false_positive"] == 0
    assert report["summary"]["false_negative"] == 0
    assert report["summary"]["matched_auto_eligible_count"] == 1
    assert report["token_usage"] == {
        "status": "reported", "prompt_tokens": 20, "completion_tokens": 8,
    }
    assert llm.calls == budget.request_count == 2
    assert json.loads((tmp_path / "report.json").read_text())["dataset_sha256"] == report["dataset_sha256"]


@pytest.mark.asyncio
async def test_extraction_quality_evaluation_stops_at_request_limit(tmp_path):
    dataset = {
        "schema_version": "memory-extraction-quality-dataset/v1",
        "dataset_id": "unit-extraction-budget",
        "cases": [{
            "id": f"case-{index}",
            "messages": [{"message_id": f"u{index}", "role": "user", "content": "我喜欢中文"}],
            "expected_facts": [],
        } for index in range(3)],
    }
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(dataset, ensure_ascii=False), encoding="utf-8")
    llm = ScriptedLLM([{"facts": []}])
    budget = EvaluationLLMBudget(llm, request_limit=1)

    report = await run_extraction_quality_evaluation(
        dataset_path, MemoryExtractor(budget), dataset_label="fixture", request_limit=1,
    )

    assert report["status"] == "incomplete"
    assert report["request_count"] == 1
    assert report["blocked_attempt_count"] == 1
    assert llm.calls == 1
    assert [case["status"] for case in report["cases"]] == ["complete", "error"]


@pytest.mark.asyncio
async def test_extraction_quality_reports_auto_eligible_precision_including_false_positives(tmp_path):
    import hashlib

    dataset = {
        "schema_version": "memory-extraction-quality-dataset/v1",
        "dataset_id": "auto-eligibility-metrics",
        "cases": [{
            "id": "language",
            "messages": [{
                "message_id": "u1", "role": "user", "content": "以后请一直用简体中文回答。",
            }],
            "expected_facts": [],
        }],
    }
    dataset_path = tmp_path / "prompts.json"
    dataset_path.write_text(json.dumps(dataset, ensure_ascii=False), encoding="utf-8")
    gold = {
        "schema_version": "memory-extraction-quality-gold/v1",
        "dataset_id": "auto-eligibility-gold",
        "source_dataset_id": dataset["dataset_id"],
        "source_dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "cases": [{"id": "language", "expected_facts": [{
            "kinds": ["preference"], "evidence_message_ids": ["u1"], "content_any": ["中文"],
        }]}],
    }
    gold_path = tmp_path / "gold.json"
    gold_path.write_text(json.dumps(gold, ensure_ascii=False), encoding="utf-8")
    predictions = {"facts": [
        fact()["facts"][0],
        {**fact(content="Prefers English replies")["facts"][0], "topic_key": "response.language"},
    ]}
    budget = EvaluationLLMBudget(ScriptedLLM([predictions]), request_limit=1)

    report = await run_extraction_quality_evaluation(
        dataset_path, MemoryExtractor(budget), dataset_label="fixture", request_limit=1,
        gold_path=gold_path,
    )

    summary = report["summary"]
    assert summary["auto_eligible_prediction_count"] == 2
    assert summary["auto_eligible_true_positive_count"] == 1
    assert summary["auto_eligible_false_positive_count"] == 1
    assert summary["auto_eligible_precision"] == 0.5


def test_extraction_quality_gold_evidence_cannot_reference_assistant_messages(tmp_path):
    dataset = {
        "schema_version": "memory-extraction-quality-dataset/v1",
        "dataset_id": "invalid-extraction-gold",
        "cases": [{
            "id": "bad-gold",
            "messages": [{"message_id": "a1", "role": "assistant", "content": ""}],
            "expected_facts": [{
                "kind": "preference", "topic_prefixes": ["response."],
                "evidence_message_ids": ["a1"], "content_any": ["x"],
            }],
        }],
    }
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(dataset), encoding="utf-8")

    with pytest.raises(ValueError, match="gold evidence must reference a user message"):
        from app.memory.extraction_quality_evaluation import ExtractionQualityDataset

        ExtractionQualityDataset.model_validate_json(path.read_text())


def test_fixed_extraction_quality_dataset_has_twenty_valid_cases():
    from app.memory.extraction_quality_evaluation import ExtractionQualityDataset

    dataset = ExtractionQualityDataset.model_validate_json(DATASET.read_bytes())
    assert len(dataset.cases) == 20
    assert sum(len(case.expected_facts) for case in dataset.cases) == 12


def test_independent_holdout_prompts_and_gold_are_separate_and_hash_bound():
    import hashlib

    from app.memory.extraction_quality_evaluation import (
        ExtractionQualityDataset,
        ExtractionQualityGold,
    )

    raw = HOLDOUT_DATASET.read_bytes()
    dataset = ExtractionQualityDataset.model_validate_json(raw)
    gold = ExtractionQualityGold.model_validate_json(HOLDOUT_GOLD.read_bytes())
    prompt_ids = {case.id for case in dataset.cases}
    gold_ids = {case.id for case in gold.cases}

    assert len(dataset.cases) == 21
    assert all(not case.expected_facts for case in dataset.cases)
    assert prompt_ids == gold_ids
    assert gold.source_dataset_id == dataset.dataset_id
    assert gold.source_dataset_sha256 == hashlib.sha256(raw).hexdigest()
    assert sum(len(case.expected_facts) for case in gold.cases) == 13


def test_saved_extraction_report_can_be_rescored_with_separate_gold_labels(tmp_path):
    import hashlib

    prompt_dataset = {
        "schema_version": "memory-extraction-quality-dataset/v1",
        "dataset_id": "prompt-set",
        "cases": [{
            "id": "case-1",
            "messages": [{"message_id": "u1", "role": "user", "content": "I prefer Chinese"}],
            "expected_facts": [],
        }],
    }
    prompt_path = tmp_path / "prompts.json"
    prompt_path.write_text(json.dumps(prompt_dataset), encoding="utf-8")
    source_dataset_hash = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    source_report = {
        "status": "complete",
        "dataset_id": "prompt-set",
        "dataset_sha256": source_dataset_hash,
        "model_label": "fixture",
        "request_count": 1,
        "request_limit": 1,
        "token_usage": {"status": "unknown", "prompt_tokens": None, "completion_tokens": None},
        "cases": [{"case_id": "case-1", "predictions": [{
            "kind": "preference", "topic_key": "response_language",
            "content": "Prefers Chinese replies", "evidence_message_ids": ["u1"],
            "confidence": 0.99,
        }]}],
    }
    report_path = tmp_path / "source-report.json"
    report_path.write_text(json.dumps(source_report), encoding="utf-8")
    gold = {
        "schema_version": "memory-extraction-quality-gold/v1",
        "dataset_id": "prompt-set-gold-v1",
        "source_dataset_id": "prompt-set",
        "source_dataset_sha256": source_dataset_hash,
        "cases": [{"id": "case-1", "expected_facts": [{
            "kinds": ["preference"], "topic_prefixes": ["response."],
            "evidence_message_ids": ["u1"], "content_any": ["Chinese"],
        }]}],
    }
    gold_path = tmp_path / "gold.json"
    gold_path.write_text(json.dumps(gold), encoding="utf-8")

    rescored = score_saved_extraction_report(
        report_path, prompt_path, gold_path, output_path=tmp_path / "rescored.json",
    )

    assert rescored["summary"]["true_positive"] == 1
    assert rescored["summary"]["false_positive"] == 0
    assert rescored["summary"]["false_negative"] == 0
    assert rescored["summary"]["auto_eligible_precision"] is None
    assert rescored["summary"]["auto_eligible_precision_status"] == "unavailable_in_source_report"
    assert rescored["request_count"] == 1
    assert json.loads((tmp_path / "rescored.json").read_text())["gold_dataset_id"] == "prompt-set-gold-v1"
