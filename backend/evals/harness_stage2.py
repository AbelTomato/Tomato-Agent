"""Offline, reproducible Harness stage 2 evaluation.

This module deliberately contains no provider or database integration.  It is
safe to run in CI and produces artifacts only in the explicitly selected RAG
reports directory.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import argparse
from time import monotonic
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.models import LLMResponse, Message
from app.agent.interfaces import LLMClient
from app.llm.budgeted_client import BudgetedRecordingLLMClient
from app.llm.compatible_client import OpenAICompatibleClient
from app.settings import Settings


TaskType = Literal["research", "writing"]
StrategyName = Literal["deterministic", "constrained_autonomous"]


class FixedTask(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    task_id: str = Field(min_length=1, max_length=100)
    task_type: TaskType
    input_text: str = Field(min_length=1, max_length=10_000)
    evidence_snapshot: str = Field(min_length=1, max_length=10_000)
    expected_evidence_refs: tuple[str, ...] = Field(max_length=100)


class HarnessEvaluationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    model_id: str = Field(min_length=1, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=200)
    runtime_version: str = Field(min_length=1, max_length=200)
    budget: dict[str, int] = Field(min_length=1)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _task_payload(tasks: tuple[FixedTask, ...]) -> list[dict[str, Any]]:
    return [task.model_dump(mode="json") for task in tasks]


def build_task_set_sha256(tasks: tuple[FixedTask, ...]) -> str:
    if not tasks:
        raise ValueError("the fixed task set must not be empty")
    ids = [task.task_id for task in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("task IDs must be unique")
    return _sha256(_task_payload(tasks))


def _snapshot_sha256(tasks: tuple[FixedTask, ...]) -> str:
    return _sha256(
        [{"task_id": task.task_id, "snapshot": task.evidence_snapshot} for task in tasks]
    )


def _case(task: FixedTask, strategy: StrategyName, config: HarnessEvaluationConfig) -> dict[str, Any]:
    autonomous = strategy == "constrained_autonomous"
    tool_calls = 1 if autonomous else 0
    model_calls = 2 if autonomous else 1
    latency_ms = 20 if autonomous else 10
    result = "completed"
    output = f"fake-{task.task_type}-{strategy}:{task.task_id}"
    return {
        "task_id": task.task_id,
        "task_type": task.task_type,
        "strategy": strategy,
        "result": result,
        "output": output,
        "error": None,
        "tool_calls": tool_calls,
        "model_calls": model_calls,
        "token_usage": {"prompt": "unknown", "completion": "unknown", "total": "unknown"},
        "latency_ms": latency_ms,
        "stop_reason": "completed",
        "budget_consumed": {
            "loops": model_calls,
            "tool_calls": tool_calls,
        },
        "evidence_refs_valid": True,
        "provenance": {
            "task_set_sha256": build_task_set_sha256((task,)),
            "snapshot_sha256": _sha256(task.evidence_snapshot),
            "model_id": config.model_id,
            "prompt_version": config.prompt_version,
            "runtime_version": config.runtime_version,
        },
    }


def run_fake_evaluation(
    tasks: tuple[FixedTask, ...], config: HarnessEvaluationConfig
) -> dict[str, Any]:
    task_hash = build_task_set_sha256(tasks)
    snapshot_hash = _snapshot_sha256(tasks)
    cases = [
        _case(task, strategy, config)
        for task in tasks
        for strategy in ("deterministic", "constrained_autonomous")
    ]
    latencies = [case["latency_ms"] for case in cases]
    metrics = {
        "task_count": len(cases),
        "completion_rate": sum(case["result"] == "completed" for case in cases) / len(cases),
        "evidence_structure_valid_rate": sum(case["evidence_refs_valid"] for case in cases)
        / len(cases),
        "tool_calls": sum(case["tool_calls"] for case in cases),
        "model_calls": sum(case["model_calls"] for case in cases),
        "token_usage": "unknown",
        "p50_latency_ms": statistics.median(latencies),
        "p95_latency_ms": max(latencies),
        "stop_reasons": {"completed": len(cases)},
    }
    return {
        "schema_version": "harness-stage-2-evaluation-v1",
        "task_set_sha256": task_hash,
        "snapshot_sha256": snapshot_hash,
        "model_id": config.model_id,
        "prompt_version": config.prompt_version,
        "runtime_version": config.runtime_version,
        "budget": config.budget,
        "metrics": metrics,
        "cases": cases,
        "provenance": {
            "provider": "fake",
            "external_requests": False,
            "task_count": len(tasks),
            "strategies": ["deterministic", "constrained_autonomous"],
        },
    }


async def run_real_provider_comparison(
    tasks: tuple[FixedTask, ...],
    *,
    model_id: str,
    prompt_version: str,
    runtime_version: str,
    provider: LLMClient,
    max_calls: int,
    response_path: Path,
) -> dict[str, Any]:
    """Run the bounded provider smoke comparison without touching business state.

    This deliberately evaluates the fixed prompt contract only. Semantic scoring,
    evidence attribution, and production-path migration remain separate gates.
    """
    if max_calls <= 0 or max_calls > 10:
        raise ValueError("max_calls must be between 1 and 10")
    task_hash = build_task_set_sha256(tasks)
    config = HarnessEvaluationConfig(
        model_id=model_id,
        prompt_version=prompt_version,
        runtime_version=runtime_version,
        budget={"max_provider_calls": max_calls},
    )
    bounded = BudgetedRecordingLLMClient(
        provider, max_calls=max_calls, output_path=response_path
    )
    cases: list[dict[str, Any]] = []
    for task in tasks:
        for strategy in ("deterministic", "constrained_autonomous"):
            started = monotonic()
            strategy_instruction = (
                "Use only the supplied evidence snapshot and answer directly."
                if strategy == "deterministic"
                else "You may propose only read-only evidence search/read actions; do not execute writes."
            )
            prompt = (
                f"Task type: {task.task_type}\n"
                f"Task: {task.input_text}\n"
                f"Evidence snapshot: {task.evidence_snapshot}\n"
                f"Expected evidence refs: {', '.join(task.expected_evidence_refs)}\n"
                f"Strategy: {strategy}\n{strategy_instruction}"
            )
            try:
                response: LLMResponse = await bounded.complete(
                    [Message(role="user", content=prompt)], []
                )
                result = "completed"
                output = response.model_dump(mode="json")
                error = None
            except Exception as exc:
                result = "failed"
                output = None
                error = str(exc)
            cases.append(
                {
                    "task_id": task.task_id,
                    "task_type": task.task_type,
                    "strategy": strategy,
                    "result": result,
                    "output": output,
                    "error": error,
                    "tool_calls": 0,
                    "model_calls": 1,
                    "token_usage": getattr(provider, "last_token_usage", "unknown")
                    or "unknown",
                    "latency_ms": round((monotonic() - started) * 1000, 2),
                    "stop_reason": "completed" if result == "completed" else "provider_error",
                    "evidence_refs_valid": None,
                    "provenance": {
                        "task_set_sha256": task_hash,
                        "snapshot_sha256": _sha256(task.evidence_snapshot),
                        "model_id": config.model_id,
                        "prompt_version": config.prompt_version,
                        "runtime_version": config.runtime_version,
                    },
                }
            )
    latencies = [case["latency_ms"] for case in cases]
    return {
        "schema_version": "harness-stage-2-evaluation-v1",
        "task_set_sha256": task_hash,
        "snapshot_sha256": _snapshot_sha256(tasks),
        "model_id": model_id,
        "prompt_version": prompt_version,
        "runtime_version": runtime_version,
        "budget": config.budget,
        "metrics": {
            "task_count": len(cases),
            "completion_rate": sum(case["result"] == "completed" for case in cases) / len(cases),
            "evidence_structure_valid_rate": "not_scored",
            "tool_calls": 0,
            "model_calls": bounded.call_count,
            "token_usage": "see cases",
            "p50_latency_ms": statistics.median(latencies),
            "p95_latency_ms": max(latencies),
            "stop_reasons": {"completed": sum(case["result"] == "completed" for case in cases)},
        },
        "cases": cases,
        "provenance": {
            "provider": "real-compatible",
            "external_requests": True,
            "task_count": len(tasks),
            "strategies": ["deterministic", "constrained_autonomous"],
            "response_path": str(response_path),
        },
    }


def write_evaluation_report(
    report: dict[str, Any], *, root: Path, run_label: str = "harness-stage-2"
) -> Path:
    root = root.resolve()
    if root.name == "agent.db" or root.suffix == ".db":
        raise ValueError("evaluation reports cannot be written to a business database path")
    output_dir = root / "2026-10-03" / run_label
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    (output_dir / "cases.jsonl").write_text(
        "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in report["cases"]),
        encoding="utf-8",
    )
    (output_dir / "provenance.json").write_text(
        json.dumps(report["provenance"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Harness stage 2 evaluation")
    parser.add_argument(
        "--real-provider",
        action="store_true",
        help="explicitly enable the configured OpenAI-compatible Provider",
    )
    parser.add_argument("--max-calls", type=int, default=10)
    args = parser.parse_args()
    from evals.harness_stage2_taskset import load_stage2_task_set

    loaded_task_set = load_stage2_task_set()
    tasks = loaded_task_set.tasks
    root = Path(__file__).resolve().parents[1] / "data" / "rag" / "reports"
    if not args.real_provider:
        config = HarnessEvaluationConfig(
            model_id="fake-harness-v1",
            prompt_version="prompt-v1",
            runtime_version="offline",
            budget={"max_loops": 3, "max_tool_calls": 2},
        )
        write_evaluation_report(run_fake_evaluation(tasks, config), root=root)
        return 0

    settings = Settings()
    if not settings.llm_api_key:
        raise SystemExit("LLM_API_KEY is required for --real-provider")
    provider = OpenAICompatibleClient(
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
    )
    real_root = root / "2026-10-03" / "harness-stage-2-real"
    report = __import__("asyncio").run(
        run_real_provider_comparison(
            tasks,
            model_id=settings.llm_model,
            prompt_version="prompt-v1",
            runtime_version="real-provider-smoke-v1",
            provider=provider,
            max_calls=args.max_calls,
            response_path=real_root / "provider-responses.jsonl",
        )
    )
    write_evaluation_report(report, root=root, run_label="harness-stage-2-real")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())