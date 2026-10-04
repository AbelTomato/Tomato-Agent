import json
from pathlib import Path

import pytest

from evals.harness_stage2 import (
    FixedTask,
    HarnessEvaluationConfig,
    build_task_set_sha256,
    run_fake_evaluation,
    write_evaluation_report,
)


def task_set() -> tuple[FixedTask, ...]:
    return (
        FixedTask(
            task_id="research-001",
            task_type="research",
            input_text="解释本地知识库中的 Harness 预算边界。",
            evidence_snapshot="snapshot-v1:budget-boundary",
            expected_evidence_refs=("chunk-budget",),
        ),
        FixedTask(
            task_id="writing-001",
            task_type="writing",
            input_text="根据证据拟定 Harness 阶段二提纲。",
            evidence_snapshot="snapshot-v1:outline",
            expected_evidence_refs=("chunk-outline",),
        ),
    )


def test_fixed_task_schema_hash_and_rejects_invalid_values() -> None:
    tasks = task_set()
    digest = build_task_set_sha256(tasks)
    assert len(digest) == 64
    assert digest == build_task_set_sha256(tasks)
    with pytest.raises(ValueError):
        FixedTask(
            task_id="bad",
            task_type="sandbox",
            input_text="x",
            evidence_snapshot="snapshot",
            expected_evidence_refs=(),
        )


def test_fake_provider_comparison_is_reproducible_and_has_machine_fields() -> None:
    config = HarnessEvaluationConfig(
        model_id="fake-harness-v1",
        prompt_version="prompt-v1",
        runtime_version="runtime-test",
        budget={"max_loops": 3, "max_tool_calls": 2},
    )
    first = run_fake_evaluation(task_set(), config)
    second = run_fake_evaluation(task_set(), config)
    assert first == second
    assert {row["strategy"] for row in first["cases"]} == {
        "deterministic",
        "constrained_autonomous",
    }
    assert first["metrics"]["task_count"] == 4
    assert "p50_latency_ms" in first["metrics"]
    assert "p95_latency_ms" in first["metrics"]
    assert all("token_usage" in row and "provenance" in row for row in first["cases"])


def test_report_writes_only_to_stage_two_rag_report_directory(tmp_path: Path) -> None:
    config = HarnessEvaluationConfig(
        model_id="fake-harness-v1",
        prompt_version="prompt-v1",
        runtime_version="runtime-test",
        budget={"max_loops": 3, "max_tool_calls": 2},
    )
    report = run_fake_evaluation(task_set(), config)
    output_dir = write_evaluation_report(report, root=tmp_path)
    assert output_dir == tmp_path / "2026-10-03" / "harness-stage-2"
    assert {path.name for path in output_dir.iterdir()} == {
        "summary.json",
        "cases.jsonl",
        "provenance.json",
    }
    summary = json.loads((output_dir / "summary.json").read_text())
    assert summary["task_set_sha256"] == build_task_set_sha256(task_set())
    assert "agent.db" not in str(output_dir)


def test_report_rejects_business_database_or_temp_like_output(tmp_path: Path) -> None:
    config = HarnessEvaluationConfig(
        model_id="fake-harness-v1",
        prompt_version="prompt-v1",
        runtime_version="runtime-test",
        budget={"max_loops": 3, "max_tool_calls": 2},
    )
    report = run_fake_evaluation(task_set(), config)
    with pytest.raises(ValueError):
        write_evaluation_report(report, root=tmp_path / "agent.db")