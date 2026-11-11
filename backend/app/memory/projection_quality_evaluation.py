"""Budgeted summary generation runs with separate human-review criteria."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.models import Message
from app.memory.extraction_quality_evaluation import EvaluationLLMBudget
from app.memory.projection import MemoryProfileSummary, build_profile_summary_prompt


class ProjectionFact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    topic: str = Field(min_length=1, max_length=200)
    fact: str = Field(min_length=1, max_length=1000)


class ProjectionReviewCriteria(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    must_preserve: list[str] = Field(default_factory=list, max_length=12)
    must_not_infer: list[str] = Field(default_factory=list, max_length=12)
    temporal_constraints: list[str] = Field(default_factory=list, max_length=8)


class ProjectionQualityCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, max_length=120)
    facts: list[ProjectionFact] = Field(min_length=1, max_length=20)
    review_criteria: ProjectionReviewCriteria

    @model_validator(mode="after")
    def validate_fact_budget(self):
        if sum(len(item.fact) for item in self.facts) > 6000:
            raise ValueError("projection_evaluation_input_too_large")
        return self


class ProjectionQualityDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: str
    dataset_id: str = Field(min_length=1)
    cases: list[ProjectionQualityCase] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_dataset(self):
        if self.schema_version != "memory-projection-quality-dataset/v1":
            raise ValueError("unsupported projection quality dataset")
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate projection quality case id")
        return self


async def run_projection_quality_evaluation(
    dataset_path: Path,
    llm,
    *,
    dataset_label: str,
    request_limit: int,
    timeout_seconds: float = 30.0,
    output_path: Path | None = None,
) -> dict[str, Any]:
    dataset_path = dataset_path.expanduser().resolve(strict=True)
    if not dataset_path.is_file() or dataset_path.is_symlink():
        raise ValueError("evaluation dataset must be a regular non-symlink file")
    raw = dataset_path.read_bytes()
    dataset = ProjectionQualityDataset.model_validate_json(raw)
    budget = llm if isinstance(llm, EvaluationLLMBudget) else None
    if budget is None:
        raise ValueError("projection evaluator must use an EvaluationLLMBudget client")
    if budget.request_limit != request_limit:
        raise ValueError("request limit does not match budget client")
    if timeout_seconds <= 0:
        raise ValueError("timeout must be positive")

    cases = []
    durations = []
    status = "complete"
    for case in dataset.cases:
        facts = [{"item_id": f"{case.id}-{index}", "version": 1,
                  "topic": item.topic, "fact": item.fact}
                 for index, item in enumerate(case.facts)]
        prompt = build_profile_summary_prompt(facts)
        started = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                budget.complete([Message(role="user", content=prompt)], []),
                timeout=timeout_seconds,
            )
            if response.kind != "final" or not response.content:
                raise ValueError("memory_projection_invalid_response")
            summary = MemoryProfileSummary.model_validate_json(response.content)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            durations.append((time.perf_counter() - started) * 1000)
            cases.append({"case_id": case.id, "status": "error", "error": type(exc).__name__})
            status = "incomplete"
            if budget.budget_exceeded:
                break
            continue
        durations.append((time.perf_counter() - started) * 1000)
        cases.append({
            "case_id": case.id,
            "status": "complete",
            "facts": facts,
            "summary": summary.summary,
            "review_criteria": case.review_criteria.model_dump(),
        })

    usage_known = budget.usage_known and budget.request_count > 0
    report = {
        "schema_version": "memory-projection-quality-report/v1",
        "status": status,
        "dataset_id": dataset.dataset_id,
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "model_label": dataset_label,
        "request_count": budget.request_count,
        "request_limit": budget.request_limit,
        "blocked_attempt_count": budget.blocked_attempt_count,
        "token_usage": {
            "status": "reported" if usage_known else "unknown",
            "prompt_tokens": budget.prompt_tokens if usage_known else None,
            "completion_tokens": budget.completion_tokens if usage_known else None,
        },
        "review_mode": "human_review_required",
        "summary": {
            "case_count": len(cases),
            "completed_case_count": sum(case["status"] == "complete" for case in cases),
            "failed_case_count": sum(case["status"] == "error" for case in cases),
            "latency_ms": {
                "p50": round(statistics.median(durations), 3) if durations else None,
                "max": round(max(durations), 3) if durations else None,
            },
        },
        "cases": cases,
    }
    if output_path is not None:
        output_path = output_path.expanduser().resolve()
        if output_path == dataset_path:
            raise ValueError("report output must not overwrite evaluation dataset")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.memory.projection_quality_evaluation")
    parser.add_argument("--dataset", type=Path, default=Path(__file__).resolve().parents[2]
                        / "evals" / "memory_projection_quality_v1.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--provider-request-limit", type=int, required=True)
    args = parser.parse_args(argv)

    from app.dependencies import create_llm_client
    from app.settings import Settings

    config = Settings()
    if not config.llm_api_key.strip() or not config.llm_model.strip():
        parser.error("projection quality evaluation requires a configured chat model")
    budget = EvaluationLLMBudget(create_llm_client(config), args.provider_request_limit)
    report = asyncio.run(run_projection_quality_evaluation(
        args.dataset, budget, dataset_label=config.llm_model,
        request_limit=args.provider_request_limit,
        timeout_seconds=min(config.llm_timeout_seconds, 60.0), output_path=args.output,
    ))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
