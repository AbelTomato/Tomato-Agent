import json
import sqlite3

import pytest

from app.errors import ProviderCallBudgetExceeded
from app.llm.compatible_client import _REQUEST_CORRELATION
from fixtures.provider_acceptance_executor import execute_scenarios
from fixtures.provider_acceptance_state import ScenarioStore
from fixtures.runtime_provider_acceptance import observed_failure
from test_execution_worker import make_worker
from test_provider_acceptance_executor import scenario
from tests.fixtures.provider_acceptance_runtime import CappedProvider


@pytest.mark.parametrize("scope", ["global", "scenario"])
async def test_budget_rejection_records_facts_without_provider_request(tmp_path, monkeypatch, scope):
    class Client:
        async def complete(self, *args, **kwargs):
            pytest.fail("budget rejection must happen before provider access")

    monkeypatch.setenv("RUNTIME_ACCEPTANCE_ROOT", str(tmp_path))
    monkeypatch.setenv("RUNTIME_ACCEPTANCE_MAX_CALLS", "1" if scope == "global" else "2")
    monkeypatch.setenv("RUNTIME_ACCEPTANCE_USED_CALLS", "1" if scope == "global" else "0")
    (tmp_path / "active-scenario.json").write_text(json.dumps({"scenario_id": "s", "request_budget": 1}))
    attempts = tmp_path / "provider-attempts.jsonl"
    attempts.write_text(json.dumps({"scenario_id": "s"}) + "\n")
    token = _REQUEST_CORRELATION.set({"run_id": "run-1", "step_id": "step-1", "call_id": "call-2"})
    try:
        with pytest.raises(ProviderCallBudgetExceeded):
            await CappedProvider(Client()).complete([], [])
    finally:
        _REQUEST_CORRELATION.reset(token)
    block = json.loads((tmp_path / "budget-events.jsonl").read_text())
    assert block["scope"] == scope
    assert block["used"] == block["limit"] == 1
    assert block["run_id"] == "run-1"
    assert len(attempts.read_text().splitlines()) == 1


@pytest.mark.parametrize("budget", [True, False])
async def test_worker_records_safe_failure_and_acceptance_retains_cause(tmp_path, caplog, budget):
    worker, runs, _, _, created = await make_worker(tmp_path)

    async def fail(run, lease, port):
        error = ProviderCallBudgetExceeded if budget else RuntimeError
        raise error("Authorization=secret-must-not-leak")

    worker.executor_factory = fail
    assert await worker.run_once()
    run = await runs.get_run(created[0].id)
    assert run.error == {"code": "worker_executor_failed"}
    facts = observed_failure(run.id, tmp_path / "runs.db")
    diagnostic = facts["execution_failures"][0]["diagnostic"]
    assert diagnostic["reason_code"] == ("provider_call_budget_exhausted" if budget else "executor_exception")
    assert diagnostic["frames"]
    assert "secret-must-not-leak" not in json.dumps(facts) + caplog.text
    item = scenario("s")

    async def observe(context):
        return {"terminal": "failed", "request_count": 1, **facts}

    item.run = observe
    result = await execute_scenarios([item], ScenarioStore(tmp_path / "summary.json"), max_runs=1, max_requests=1)
    assert result["s"]["error_category"] == ("provider_call_budget_exhausted" if budget else "execution_failed")
    assert result["s"]["actual_observations"]["assertion_failure"] == "expectation_mismatch"


async def test_budget_diagnostic_is_correlated_and_classified_in_summary(tmp_path):
    database = tmp_path / "runs.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE task_run_events (run_id TEXT, event_type TEXT, sequence INTEGER, payload TEXT)")
    blocks = [{"run_id": run_id, "reason_code": "provider_call_budget_exhausted"} for run_id in ("run-1", "other")]
    (tmp_path / "budget-events.jsonl").write_text("\n".join(json.dumps(block) for block in blocks))
    facts = observed_failure("run-1", database)
    assert facts["budget_blocks"] == [blocks[0]]
    item = scenario("s")

    async def observe(context):
        return {"terminal": "failed", "request_count": 1, **facts}

    item.run = observe
    result = await execute_scenarios([item], ScenarioStore(tmp_path / "summary.json"), max_runs=1, max_requests=1)
    assert result["s"]["error_category"] == "provider_call_budget_exhausted"
    assert result["s"]["failed_phase"] == "budget"