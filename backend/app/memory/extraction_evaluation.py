"""Offline evaluation of the automatic-acceptance policy gate, not model extraction quality."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.memory.extraction import ExtractedFact, MemoryExtractor, _SENSITIVE


class ExtractionPolicyCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    content: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    self_reported: bool
    durable: bool
    expected_auto: bool
    category: str = Field(min_length=1)


class ExtractionPolicyDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: str
    dataset_id: str = Field(min_length=1)
    cases: list[ExtractionPolicyCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_case_ids(self):
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate extraction case id")
        if self.schema_version != "memory-extraction-policy-dataset/v1":
            raise ValueError("unsupported extraction policy dataset")
        return self


def run_extraction_policy_evaluation(dataset_path: Path) -> dict:
    dataset_path = dataset_path.expanduser().resolve(strict=True)
    if not dataset_path.is_file() or dataset_path.is_symlink():
        raise ValueError("evaluation dataset must be a regular non-symlink file")
    raw = dataset_path.read_bytes()
    dataset = ExtractionPolicyDataset.model_validate_json(raw)
    rows = []
    tp = fp = tn = fn = 0
    for case in dataset.cases:
        fact = ExtractedFact(
            kind="preference", topic_key="evaluation.policy", content=case.content,
            evidence_message_ids=[case.id], confidence=case.confidence,
            self_reported=case.self_reported, durable=case.durable,
        )
        actual = (
            MemoryExtractor.auto_eligible(fact, case.evidence)
            and not _SENSITIVE.search(case.content)
            and not _SENSITIVE.search(case.evidence)
        )
        tp += int(actual and case.expected_auto)
        fp += int(actual and not case.expected_auto)
        tn += int(not actual and not case.expected_auto)
        fn += int(not actual and case.expected_auto)
        rows.append({
            "case_id": case.id,
            "category": case.category,
            "expected_auto": case.expected_auto,
            "actual_auto": actual,
            "correct": actual == case.expected_auto,
        })
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {
        "schema_version": "memory-extraction-policy-report/v1",
        "dataset_id": dataset.dataset_id,
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "scope": "automatic-acceptance gate only; model extraction quality is not measured",
        "summary": {
            "case_count": len(rows), "true_positive": tp, "false_positive": fp,
            "true_negative": tn, "false_negative": fn,
            "precision": round(precision, 4), "recall": round(recall, 4),
            "accuracy": round((tp + tn) / max(1, len(rows)), 4),
        },
        "cases": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.memory.extraction_evaluation")
    parser.add_argument("--dataset", type=Path, default=Path(__file__).resolve().parents[2]
                        / "evals" / "memory_extraction_policy_v1.json")
    args = parser.parse_args(argv)
    print(json.dumps(run_extraction_policy_evaluation(args.dataset), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
