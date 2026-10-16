import asyncio

import pytest

from fixtures.provider_acceptance_executor import (
    AcceptanceScenario,
    count_recorded_calls,
    execute_scenarios,
)
from fixtures.provider_acceptance_state import ScenarioRecord, ScenarioStore


def scenario(name, *, budget=2, expected_terminal="completed", expected_operations=()):
    return AcceptanceScenario(
        record=ScenarioRecord(
            acceptance_id="acceptance-1",
            scenario_id=name,
            timeout_seconds=1,
            request_budget=budget,
            expected_terminal=expected_terminal,
            expected_operations=list(expected_operations),
        ),
        run=lambda context: asyncio.sleep(0, result={
            "terminal": expected_terminal,
            "events": [],
            "operations": list(expected_operations),
            "request_count": 1,
        }),
    )


@pytest.mark.asyncio
async def test_scenario_passes_only_when_declared_terminal_events_and_operations_match(tmp_path):
    store = ScenarioStore(tmp_path / "summary.json")
    first = scenario("clarification", expected_operations=["clarify"])
    first.run = lambda context: asyncio.sleep(0, result={
        "run_id": "run-1",
        "terminal": "completed",
        "events": ["assistant.waiting", "run.completed"],
        "operations": ["clarify"],
        "request_count": 1,
    })

    results = await execute_scenarios([first], store, max_runs=1, max_requests=1)

    assert results["clarification"]["status"] == "passed"
    assert results["clarification"]["run_id"] == "run-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "timed_out", "cancelled", "budget_exhausted"])
async def test_non_pass_outcome_stops_before_later_scenarios(tmp_path, outcome):
    store = ScenarioStore(tmp_path / "summary.json")
    started = []

    async def stop(context):
        started.append("first")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        if outcome == "timed_out":
            await asyncio.sleep(0.02)
        if outcome == "budget_exhausted":
            return {"terminal": "completed", "events": [], "operations": [], "request_count": 2}
        raise RuntimeError("provider_failure")

    first = AcceptanceScenario(
        record=ScenarioRecord(
            acceptance_id="acceptance-1", scenario_id="first", timeout_seconds=0.001,
            request_budget=1,
        ),
        run=stop,
    )
    second = scenario("second")
    second.run = lambda context: started.append("second") or asyncio.sleep(0)

    results = await execute_scenarios([first, second], store, max_runs=2, max_requests=2)

    assert "second" not in started
    assert results["first"]["status"] in {"failed", "timed_out", "cancelled"}
    assert store.get("first")["status"] == results["first"]["status"]
    assert store.get("first")["ended_at"]
    assert "second" not in store.scenario_ids


@pytest.mark.asyncio
async def test_runtime_exception_is_recorded_as_scenario_failure_not_assertion_mismatch(tmp_path):
    store = ScenarioStore(tmp_path / "summary.json")

    async def fail(context):
        context.consume_request()
        raise RuntimeError("LLM provider returned an empty content response")

    item = AcceptanceScenario(
        record=ScenarioRecord(
            acceptance_id="acceptance-1", scenario_id="parse_failure", timeout_seconds=1,
            request_budget=2, expected_terminal="completed",
            expected_events=["run.completed"],
        ),
        run=fail,
    )

    results = await execute_scenarios([item], store, max_runs=1, max_requests=2)
    result = results["parse_failure"]

    assert result["status"] == "failed"
    assert result["failed_phase"] == "scenario"
    assert result["error_category"] == "RuntimeError"
    assert result["actual_observations"] == {
        "request_count": 1,
        "error_type": "RuntimeError",
        "error": "LLM provider returned an empty content response",
    }
    assert result["request_count"] == 1


@pytest.mark.asyncio
async def test_assertion_failure_keeps_observed_events_separate_from_expectations(tmp_path):
    store = ScenarioStore(tmp_path / "summary.json")
    item = scenario("missing_event")
    item.record.expected_events = ["run.completed"]
    item.run = lambda context: asyncio.sleep(0, result={
        "terminal": "failed",
        "events": ["clarification.waiting"],
        "operations": [],
        "request_count": 1,
    })

    result = (await execute_scenarios([item], store, max_runs=1, max_requests=1))["missing_event"]

    assert result["status"] == "failed"
    assert result["failed_phase"] == "assertions"
    assert result["error_category"] == "expectation_mismatch"
    assert result["expected_events"] == ["run.completed"]
    assert result["actual_observations"]["events"] == ["clarification.waiting"]


@pytest.mark.asyncio
async def test_global_budgets_reject_before_starting_scenario(tmp_path):
    store = ScenarioStore(tmp_path / "summary.json")
    store.add(ScenarioRecord(acceptance_id="acceptance-1", scenario_id="prior"))
    store.transition("prior", "running")
    store.transition("prior", "passed")
    started = False

    async def run(context):
        nonlocal started
        started = True
        return {"terminal": "completed", "events": [], "operations": [], "request_count": 1}

    item = scenario("next")
    item.run = run

    results = await execute_scenarios([item], store, max_runs=1, max_requests=4)

    assert not started
    assert results["next"]["status"] == "failed"
    assert results["next"]["error_category"] == "global_run_budget_exhausted"


@pytest.mark.asyncio
async def test_global_request_budget_includes_previous_scenario_usage(tmp_path):
    store = ScenarioStore(tmp_path / "summary.json")
    previous = scenario("prior", budget=2)
    previous.run = lambda context: asyncio.sleep(0, result={
        "terminal": "completed", "events": [], "operations": [], "request_count": 2,
    })
    next_started = False

    async def next_run(context):
        nonlocal next_started
        next_started = True
        return {"terminal": "completed", "events": [], "operations": [], "request_count": 1}

    following = scenario("following")
    following.run = next_run
    results = await execute_scenarios([previous, following], store, max_runs=3, max_requests=2)

    assert not next_started
    assert results["prior"]["status"] == "passed"
    assert results["following"]["error_category"] == "global_request_budget_exhausted"


@pytest.mark.asyncio
async def test_scenario_request_budget_records_only_declared_limit(tmp_path):
    store = ScenarioStore(tmp_path / "summary.json")
    item = scenario("limited", budget=1)
    item.run = lambda context: asyncio.sleep(0, result={
        "terminal": "completed", "events": [], "operations": [], "request_count": 2,
    })

    results = await execute_scenarios([item, scenario("never")], store, max_runs=3, max_requests=5)

    assert results["limited"]["status"] == "failed"
    assert results["limited"]["error_category"] == "request_budget_exhausted"
    assert results["limited"]["request_count"] == 1
    assert "never" not in results


def test_recorded_call_counter_filters_scenario_and_ignores_blank_lines(tmp_path):
    path = tmp_path / "provider-attempts.jsonl"
    path.write_text(
        '{"scenario_id":"one"}\n\n{"scenario_id":"two"}\n{"scenario_id":"one"}\n',
        encoding="utf-8",
    )

    assert count_recorded_calls(path) == 3
    assert count_recorded_calls(path, "one") == 2
    assert count_recorded_calls(path, "missing") == 0