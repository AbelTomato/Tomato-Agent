"""Budgeted evaluation of model-extracted facts against a synthetic gold set."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.memory.extraction import ExtractedFact, MemoryExtractor, _SENSITIVE


class EvalMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    message_id: str = Field(min_length=1, max_length=160)
    role: Literal["user", "assistant"]
    content: str = Field(max_length=6000)


class ExpectedFact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["preference", "habit", "background", "episodic"]
    topic_prefixes: list[str] = Field(min_length=1, max_length=8)
    evidence_message_ids: list[str] = Field(min_length=1, max_length=8)
    content_any: list[str] = Field(min_length=1, max_length=8)


class ExtractionQualityCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1)
    messages: list[EvalMessage] = Field(min_length=1, max_length=40)
    expected_facts: list[ExpectedFact] = Field(max_length=12)

    @model_validator(mode="after")
    def validate_evidence_references(self):
        messages = {message.message_id: message for message in self.messages}
        if len(messages) != len(self.messages):
            raise ValueError("duplicate extraction evaluation message id")
        for fact in self.expected_facts:
            for message_id in fact.evidence_message_ids:
                if message_id not in messages or messages[message_id].role != "user":
                    raise ValueError("gold evidence must reference a user message")
        return self


class ExtractionQualityDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: str
    dataset_id: str = Field(min_length=1)
    cases: list[ExtractionQualityCase] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_dataset(self):
        if self.schema_version != "memory-extraction-quality-dataset/v1":
            raise ValueError("unsupported extraction quality dataset")
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate extraction quality case id")
        return self


class GoldFact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kinds: list[Literal["preference", "habit", "background", "episodic"]] = Field(min_length=1)
    topic_prefixes: list[str] = Field(default_factory=list)
    evidence_message_ids: list[str] = Field(min_length=1)
    content_any: list[str] = Field(min_length=1)
    content_all: list[str] = Field(default_factory=list)


class GoldCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    expected_facts: list[GoldFact] = Field(max_length=12)


class ExtractionQualityGold(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: str
    dataset_id: str
    source_dataset_id: str
    source_dataset_sha256: str = Field(min_length=64, max_length=64)
    cases: list[GoldCase] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_gold(self):
        if self.schema_version != "memory-extraction-quality-gold/v1":
            raise ValueError("unsupported extraction quality gold set")
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate extraction quality gold case id")
        return self


class EvaluationLLMBudget:
    def __init__(self, client, request_limit: int):
        if request_limit < 1:
            raise ValueError("provider request limit must be positive")
        self.client = client
        self.request_limit = request_limit
        self.request_count = 0
        self.blocked_attempt_count = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.usage_known = True
        self.budget_exceeded = False

    async def complete(self, messages, tools):
        if self.request_count >= self.request_limit:
            self.blocked_attempt_count += 1
            self.budget_exceeded = True
            raise RuntimeError("memory_evaluation_provider_request_limit")
        self.request_count += 1
        if hasattr(self.client, "last_token_usage"):
            self.client.last_token_usage = None
        response = await self.client.complete(messages, tools)
        usage = getattr(self.client, "last_token_usage", None)
        if usage is None:
            self.usage_known = False
        else:
            self.prompt_tokens += usage.get("prompt_tokens", 0)
            self.completion_tokens += usage.get("completion_tokens", 0)
        return response


def _matches(prediction: ExtractedFact, expected: ExpectedFact) -> bool:
    return (
        prediction.kind == expected.kind
        and any(prediction.topic_key.startswith(prefix) for prefix in expected.topic_prefixes)
        and set(prediction.evidence_message_ids) == set(expected.evidence_message_ids)
        and any(token.casefold() in prediction.content.casefold() for token in expected.content_any)
    )


def _matches_gold(prediction: ExtractedFact, expected: GoldFact) -> bool:
    topic = prediction.topic_key.casefold().replace("_", ".").replace("-", ".")
    prefixes = [prefix.casefold().replace("_", ".").replace("-", ".")
                for prefix in expected.topic_prefixes]
    content = prediction.content.casefold()
    return (
        prediction.kind in expected.kinds
        and (not prefixes or any(topic.startswith(prefix) for prefix in prefixes))
        and set(prediction.evidence_message_ids) == set(expected.evidence_message_ids)
        and any(token.casefold() in content for token in expected.content_any)
        and all(token.casefold() in content for token in expected.content_all)
    )


def score_saved_extraction_report(
    source_report_path: Path,
    prompt_dataset_path: Path,
    gold_path: Path,
    *,
    output_path: Path | None = None,
) -> dict:
    source_report_path = source_report_path.expanduser().resolve(strict=True)
    prompt_dataset_path = prompt_dataset_path.expanduser().resolve(strict=True)
    gold_path = gold_path.expanduser().resolve(strict=True)
    source_raw = source_report_path.read_bytes()
    prompt_raw = prompt_dataset_path.read_bytes()
    gold_raw = gold_path.read_bytes()
    source = json.loads(source_raw)
    dataset = ExtractionQualityDataset.model_validate_json(prompt_raw)
    gold = ExtractionQualityGold.model_validate_json(gold_raw)
    source_dataset_hash = hashlib.sha256(prompt_raw).hexdigest()
    if source.get("status") != "complete":
        raise ValueError("cannot rescore an incomplete extraction evaluation")
    if source.get("dataset_id") != gold.source_dataset_id or source.get("dataset_sha256") != gold.source_dataset_sha256:
        raise ValueError("gold set does not match the evaluated prompt dataset")
    if source_dataset_hash != gold.source_dataset_sha256:
        raise ValueError("prompt dataset content differs from the source run")
    dataset_cases = {case.id: case for case in dataset.cases}
    gold_cases = {case.id: case for case in gold.cases}
    source_cases = {case["case_id"]: case for case in source.get("cases", [])}
    if set(dataset_cases) != set(gold_cases) or set(dataset_cases) != set(source_cases):
        raise ValueError("prompt, gold, and source report cases do not match")

    rows = []
    total_tp = total_fp = total_fn = 0
    unsupported = sensitive_count = matched_count = 0
    auto_eligible_predictions = auto_eligible_tp = auto_eligible_fp = 0
    auto_eligibility_available = True
    for case_id, case in dataset_cases.items():
        expected = list(gold_cases[case_id].expected_facts)
        predictions = []
        for saved_prediction in source_cases[case_id].get("predictions", []):
            if "self_reported" not in saved_prediction or "durable" not in saved_prediction:
                auto_eligibility_available = False
            prediction = {key: value for key, value in saved_prediction.items()
                          if key in {"kind", "topic_key", "content", "evidence_message_ids",
                                     "confidence", "self_reported", "durable"}}
            prediction.setdefault("self_reported", False)
            prediction.setdefault("durable", False)
            predictions.append(ExtractedFact.model_validate(prediction))
        remaining = expected.copy()
        prediction_rows = []
        case_tp = case_fp = 0
        user_messages = {message.message_id: message.content
                         for message in case.messages if message.role == "user"}
        for prediction in predictions:
            if not set(prediction.evidence_message_ids) <= set(user_messages):
                unsupported += 1
            evidence = "\n".join(user_messages.get(message_id, "")
                                  for message_id in prediction.evidence_message_ids)
            sensitive = bool(_SENSITIVE.search(prediction.content) or _SENSITIVE.search(evidence))
            sensitive_count += int(sensitive)
            eligible = (
                MemoryExtractor.auto_eligible(prediction, evidence)
                and not sensitive
                and set(prediction.evidence_message_ids) <= set(user_messages)
            )
            match = next((fact for fact in remaining if _matches_gold(prediction, fact)), None)
            if match is None:
                case_fp += 1
                if eligible:
                    auto_eligible_fp += 1
            else:
                remaining.remove(match)
                case_tp += 1
                matched_count += 1
                if eligible:
                    auto_eligible_tp += 1
            if eligible:
                auto_eligible_predictions += 1
            prediction_rows.append({
                "kind": prediction.kind, "topic_key": prediction.topic_key,
                "content": prediction.content,
                "evidence_message_ids": prediction.evidence_message_ids,
                "matched_gold": match is not None,
            })
        case_fn = len(remaining)
        total_tp += case_tp
        total_fp += case_fp
        total_fn += case_fn
        rows.append({
            "case_id": case_id, "expected_fact_count": len(expected),
            "predicted_fact_count": len(predictions), "true_positive": case_tp,
            "false_positive": case_fp, "false_negative": case_fn,
            "predictions": prediction_rows,
        })

    precision = total_tp / (total_tp + total_fp) if total_tp + total_fp else 1.0
    recall = total_tp / (total_tp + total_fn) if total_tp + total_fn else 1.0
    auto_eligible_precision = (
        round(auto_eligible_tp / auto_eligible_predictions, 4)
        if auto_eligibility_available and auto_eligible_predictions else None
    )
    report = {
        "schema_version": "memory-extraction-quality-rescore/v1",
        "status": "complete",
        "source_report_sha256": hashlib.sha256(source_raw).hexdigest(),
        "source_dataset_id": source["dataset_id"],
        "source_dataset_sha256": source_dataset_hash,
        "gold_dataset_id": gold.dataset_id,
        "gold_dataset_sha256": hashlib.sha256(gold_raw).hexdigest(),
        "model_label": source["model_label"],
        "request_count": source["request_count"],
        "request_limit": source["request_limit"],
        "token_usage": source["token_usage"],
        "summary": {
            "case_count": len(rows), "expected_fact_count": total_tp + total_fn,
            "predicted_fact_count": total_tp + total_fp, "true_positive": total_tp,
            "false_positive": total_fp, "false_negative": total_fn,
            "precision": round(precision, 4), "recall": round(recall, 4),
            "f1": round(2 * precision * recall / (precision + recall), 4)
            if precision + recall else 0.0,
            "unsupported_evidence_count": unsupported,
            "sensitive_output_count": sensitive_count,
            "matched_auto_eligible_count": auto_eligible_tp if auto_eligibility_available else None,
            "matched_auto_eligible_status": (
                "reported" if auto_eligibility_available else "unavailable_in_source_report"
            ),
            "auto_eligible_prediction_count": (
                auto_eligible_predictions if auto_eligibility_available else None
            ),
            "auto_eligible_true_positive_count": auto_eligible_tp if auto_eligibility_available else None,
            "auto_eligible_false_positive_count": auto_eligible_fp if auto_eligibility_available else None,
            "auto_eligible_precision": auto_eligible_precision,
            "auto_eligible_precision_status": (
                "reported" if auto_eligibility_available and auto_eligible_predictions
                else "no_eligible_predictions" if auto_eligibility_available
                else "unavailable_in_source_report"
            ),
            "matched_expected_count": matched_count,
        },
        "cases": rows,
    }
    if output_path is not None:
        output_path = output_path.expanduser().resolve()
        if output_path in {source_report_path, prompt_dataset_path, gold_path}:
            raise ValueError("rescore output must not overwrite an input")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


async def run_extraction_quality_evaluation(
    dataset_path: Path,
    extractor: MemoryExtractor,
    *,
    dataset_label: str,
    request_limit: int,
    gold_path: Path | None = None,
    output_path: Path | None = None,
) -> dict:
    dataset_path = dataset_path.expanduser().resolve(strict=True)
    if not dataset_path.is_file() or dataset_path.is_symlink():
        raise ValueError("evaluation dataset must be a regular non-symlink file")
    raw = dataset_path.read_bytes()
    dataset = ExtractionQualityDataset.model_validate_json(raw)
    gold = None
    gold_raw = None
    gold_cases = {}
    if gold_path is not None:
        gold_path = gold_path.expanduser().resolve(strict=True)
        gold_raw = gold_path.read_bytes()
        gold = ExtractionQualityGold.model_validate_json(gold_raw)
        if (gold.source_dataset_id != dataset.dataset_id
                or gold.source_dataset_sha256 != hashlib.sha256(raw).hexdigest()):
            raise ValueError("gold set does not match extraction prompt dataset")
        gold_cases = {case.id: case for case in gold.cases}
        if set(gold_cases) != {case.id for case in dataset.cases}:
            raise ValueError("gold set cases do not match extraction prompt dataset")
    rows = []
    durations = []
    total_tp = total_fp = total_fn = 0
    unsupported_evidence_count = 0
    sensitive_output_count = 0
    matched_auto_eligible_count = 0
    matched_expected_count = 0
    auto_eligible_prediction_count = 0
    auto_eligible_true_positive_count = 0
    auto_eligible_false_positive_count = 0
    budget = getattr(extractor, "llm", None)
    if not isinstance(budget, EvaluationLLMBudget):
        raise ValueError("extractor must use an EvaluationLLMBudget client")
    if budget.request_limit != request_limit:
        raise ValueError("request limit does not match budget client")

    status = "complete"
    for case in dataset.cases:
        started = time.perf_counter()
        try:
            result = await extractor.extract([message.model_dump() for message in case.messages])
        except Exception as exc:
            durations.append((time.perf_counter() - started) * 1000)
            rows.append({"case_id": case.id, "status": "error", "error": type(exc).__name__})
            status = "incomplete"
            if budget.budget_exceeded:
                break
            continue
        durations.append((time.perf_counter() - started) * 1000)

        expected_facts = gold_cases[case.id].expected_facts if gold is not None else case.expected_facts
        remaining = list(expected_facts)
        predictions = []
        case_tp = case_fp = 0
        for prediction in result.facts:
            valid_user_ids = {message.message_id for message in case.messages if message.role == "user"}
            if not set(prediction.evidence_message_ids) <= valid_user_ids:
                unsupported_evidence_count += 1
            evidence_text = "\n".join(
                message.content for message in case.messages
                if message.role == "user" and message.message_id in prediction.evidence_message_ids
            )
            sensitive = bool(_SENSITIVE.search(prediction.content) or _SENSITIVE.search(evidence_text))
            sensitive_output_count += int(sensitive)
            auto_eligible = (
                MemoryExtractor.auto_eligible(prediction, evidence_text)
                and not sensitive
                and set(prediction.evidence_message_ids) <= valid_user_ids
            )
            matcher = _matches_gold if gold is not None else _matches
            matched = next((gold_fact for gold_fact in remaining
                            if matcher(prediction, gold_fact)), None)
            if matched is None:
                case_fp += 1
                auto_eligible_false_positive_count += int(auto_eligible)
            else:
                remaining.remove(matched)
                case_tp += 1
                matched_expected_count += 1
                matched_auto_eligible_count += int(auto_eligible)
                auto_eligible_true_positive_count += int(auto_eligible)
            auto_eligible_prediction_count += int(auto_eligible)
            predictions.append({
                "kind": prediction.kind,
                "topic_key": prediction.topic_key,
                "content": prediction.content,
                "evidence_message_ids": prediction.evidence_message_ids,
                "confidence": prediction.confidence,
                "self_reported": prediction.self_reported,
                "durable": prediction.durable,
                "matched_gold": matched is not None,
            })
        case_fn = len(remaining)
        total_tp += case_tp
        total_fp += case_fp
        total_fn += case_fn
        rows.append({
            "case_id": case.id,
            "status": "complete",
            "expected_fact_count": len(expected_facts),
            "predicted_fact_count": len(result.facts),
            "true_positive": case_tp,
            "false_positive": case_fp,
            "false_negative": case_fn,
            "predictions": predictions,
        })

    precision = total_tp / (total_tp + total_fp) if total_tp + total_fp else 1.0
    recall = total_tp / (total_tp + total_fn) if total_tp + total_fn else 1.0
    usage_known = budget.usage_known and budget.request_count > 0
    auto_eligible_precision = (
        round(auto_eligible_true_positive_count / auto_eligible_prediction_count, 4)
        if auto_eligible_prediction_count else None
    )
    report = {
        "schema_version": "memory-extraction-quality-report/v1",
        "status": status,
        "dataset_id": dataset.dataset_id,
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "gold_dataset_id": gold.dataset_id if gold is not None else None,
        "gold_dataset_sha256": hashlib.sha256(gold_raw).hexdigest() if gold_raw is not None else None,
        "model_label": dataset_label,
        "request_count": budget.request_count,
        "request_limit": budget.request_limit,
        "blocked_attempt_count": budget.blocked_attempt_count,
        "token_usage": {
            "status": "reported" if usage_known else "unknown",
            "prompt_tokens": budget.prompt_tokens if usage_known else None,
            "completion_tokens": budget.completion_tokens if usage_known else None,
        },
        "summary": {
            "case_count": len(rows),
            "expected_fact_count": total_tp + total_fn,
            "predicted_fact_count": total_tp + total_fp,
            "true_positive": total_tp,
            "false_positive": total_fp,
            "false_negative": total_fn,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(2 * precision * recall / (precision + recall), 4)
            if precision + recall else 0.0,
            "unsupported_evidence_count": unsupported_evidence_count,
            "sensitive_output_count": sensitive_output_count,
            "matched_auto_eligible_count": matched_auto_eligible_count,
            "matched_expected_count": matched_expected_count,
            "auto_eligible_prediction_count": auto_eligible_prediction_count,
            "auto_eligible_true_positive_count": auto_eligible_true_positive_count,
            "auto_eligible_false_positive_count": auto_eligible_false_positive_count,
            "auto_eligible_precision": auto_eligible_precision,
            "auto_eligible_precision_status": (
                "reported" if auto_eligible_prediction_count else "no_eligible_predictions"
            ),
            "latency_ms": {
                "p50": round(statistics.median(durations), 3) if durations else None,
                "max": round(max(durations), 3) if durations else None,
            },
        },
        "cases": rows,
    }
    if output_path is not None:
        output_path = output_path.expanduser().resolve()
        if output_path == dataset_path:
            raise ValueError("report output must not overwrite evaluation dataset")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.memory.extraction_quality_evaluation")
    parser.add_argument("--dataset", type=Path, default=Path(__file__).resolve().parents[2]
                        / "evals" / "memory_extraction_quality_v1.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--provider-request-limit", type=int)
    parser.add_argument("--rescore-report", type=Path)
    parser.add_argument("--gold-dataset", type=Path, default=Path(__file__).resolve().parents[2]
                        / "evals" / "memory_extraction_quality_gold_v2.json")
    args = parser.parse_args(argv)

    if args.rescore_report is not None:
        report = score_saved_extraction_report(
            args.rescore_report, args.dataset, args.gold_dataset, output_path=args.output,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.provider_request_limit is None:
        parser.error("--provider-request-limit is required when calling a model")

    from app.dependencies import create_llm_client
    from app.settings import Settings

    config = Settings()
    if not config.llm_api_key.strip() or not config.llm_model.strip():
        parser.error("extraction quality evaluation requires a configured chat model")
    client = EvaluationLLMBudget(create_llm_client(config), args.provider_request_limit)
    extractor = MemoryExtractor(client, timeout_seconds=min(config.llm_timeout_seconds, 60.0))
    report = asyncio.run(run_extraction_quality_evaluation(
        args.dataset, extractor, dataset_label=config.llm_model,
        request_limit=args.provider_request_limit, gold_path=args.gold_dataset,
        output_path=args.output,
    ))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
