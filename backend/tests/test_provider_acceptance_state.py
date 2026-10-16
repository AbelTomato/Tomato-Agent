import json

import pytest

from fixtures.provider_acceptance_state import (
    AcceptanceStateError,
    ScenarioRecord,
    ScenarioStore,
)


def scenario(**overrides):
    values = {
        "acceptance_id": "acceptance-1",
        "scenario_id": "scenario-1",
        "run_id": None,
        "timeout_seconds": 120,
        "request_budget": 2,
        "expected_terminal": "completed",
        "expected_events": ["assistant.message", "run.completed"],
        "expected_operations": ["search_knowledge"],
    }
    values.update(overrides)
    return ScenarioRecord(**values)


def test_legal_transitions_persist_and_terminal_cannot_reopen(tmp_path):
    store = ScenarioStore(tmp_path / "summary.json")
    record = scenario()

    store.add(record)
    store.transition(record.scenario_id, "running", run_id="run-1")
    store.transition(record.scenario_id, "passed", actual_observations={"status": "completed"})

    saved = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    result = saved["scenarios"][record.scenario_id]
    assert result["acceptance_id"] == "acceptance-1"
    assert result["run_id"] == "run-1"
    assert result["status"] == "passed"
    assert result["started_at"]
    assert result["ended_at"]
    assert result["timeout_seconds"] == 120
    assert result["request_budget"] == 2
    assert result["expected_events"] == ["assistant.message", "run.completed"]
    assert result["expected_operations"] == ["search_knowledge"]

    with pytest.raises(AcceptanceStateError):
        store.transition(record.scenario_id, "running")


@pytest.mark.parametrize(
    ("initial", "target"),
    [("pending", "passed"), ("running", "pending"), ("failed", "passed")],
)
def test_illegal_transitions_are_rejected(tmp_path, initial, target):
    store = ScenarioStore(tmp_path / "summary.json")
    record = scenario()
    store.add(record)
    if initial != "pending":
        store.transition(record.scenario_id, "running")
    if initial == "failed":
        store.transition(record.scenario_id, "failed")

    with pytest.raises(AcceptanceStateError):
        store.transition(record.scenario_id, target)


def test_request_budget_rejects_before_call_and_failure_keeps_diagnostics(tmp_path):
    store = ScenarioStore(tmp_path / "summary.json")
    record = scenario(request_budget=1)
    store.add(record)
    store.transition(record.scenario_id, "running")
    store.consume_request(record.scenario_id)

    with pytest.raises(AcceptanceStateError, match="request_budget_exhausted"):
        store.consume_request(record.scenario_id)

    store.update(record.scenario_id, diagnostic_files={"provider_attempts": "attempts.jsonl"})
    store.transition(
        record.scenario_id,
        "failed",
        failed_phase="provider_call",
        error_category="timeout",
    )
    result = store.get(record.scenario_id)
    assert result["request_count"] == 1
    assert result["diagnostic_files"] == {"provider_attempts": "attempts.jsonl"}
    assert result["failed_phase"] == "provider_call"
    assert result["error_category"] == "timeout"


def test_atomic_incremental_save_and_running_scenario_recovery(tmp_path):
    path = tmp_path / "summary.json"
    store = ScenarioStore(path)
    record = scenario()
    store.add(record)
    store.transition(record.scenario_id, "running", run_id="run-2")
    store.consume_request(record.scenario_id)
    store.update(record.scenario_id, diagnostic_files={"events": "events.jsonl"})
    last_updated = store.get(record.scenario_id)["updated_at"]

    recovered = ScenarioStore(path)
    recovered.recover_running()

    result = recovered.get(record.scenario_id)
    assert result["status"] == "interrupted"
    assert result["run_id"] == "run-2"
    assert result["request_count"] == 1
    assert result["updated_at"] >= last_updated
    assert result["ended_at"]
    assert result["diagnostic_files"] == {"events": "events.jsonl"}
    assert not list(tmp_path.glob("*.tmp"))
