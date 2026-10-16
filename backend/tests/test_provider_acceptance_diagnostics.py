import json
import asyncio

import pytest

from tests.fixtures.provider_acceptance_diagnostics import collect_ui_diagnostics
from tests.fixtures.provider_acceptance_executor import AcceptanceScenario, execute_scenarios
from tests.fixtures.provider_acceptance_state import ScenarioRecord, ScenarioStore


class FakePage:
    url = "http://127.0.0.1:5190/"

    async def title(self):
        return "Acceptance"

    def locator(self, selector):
        assert selector == "body"
        return self

    async def inner_text(self, timeout):
        return "Run status: failed\n" + ("details " * 2000)

    async def screenshot(self, path, full_page):
        assert full_page is True
        from pathlib import Path
        Path(path).write_bytes(b"png")


class ScreenshotFailurePage(FakePage):
    async def screenshot(self, path, full_page):
        raise RuntimeError("screenshot unavailable")


@pytest.mark.asyncio
async def test_collects_bounded_page_snapshot_and_screenshot(tmp_path):
    result = await collect_ui_diagnostics(
        FakePage(), tmp_path, scenario_id="scenario-1", visible_run_status="running"
    )

    assert result["snapshot"]["scenario_id"] == "scenario-1"
    assert result["snapshot"]["visible_run_status"] == "running"
    assert len(result["snapshot"]["page_text"]) <= 4000
    assert result["diagnostic_files"]["screenshot"].endswith("scenario-1.png")
    assert (tmp_path / "scenario-1.png").read_bytes() == b"png"
    assert result["diagnostic_errors"] == []


@pytest.mark.asyncio
async def test_screenshot_failure_is_separate_from_original_scenario_error(tmp_path):
    original_error = {"error_category": "RuntimeError", "error": "provider failed"}
    result = await collect_ui_diagnostics(
        ScreenshotFailurePage(), tmp_path, scenario_id="scenario-2",
        original_error=original_error,
    )

    assert result["original_error"] == original_error
    assert result["diagnostic_errors"][-1] == {
        "source": "screenshot", "error_type": "RuntimeError", "error": "screenshot unavailable"
    }
    assert result["snapshot"]["page_title"] == "Acceptance"
    saved = json.loads((tmp_path / "scenario-2-ui.json").read_text(encoding="utf-8"))
    assert saved["original_error"] == original_error


@pytest.mark.asyncio
async def test_page_inspection_failure_does_not_raise_or_drop_diagnostic_record(tmp_path):
    class BrokenPage:
        @property
        def url(self):
            raise RuntimeError("CDP disconnected")

    result = await collect_ui_diagnostics(BrokenPage(), tmp_path, scenario_id="scenario-3")

    assert result["diagnostic_errors"][0]["source"] == "page_url"
    assert result["snapshot"]["page_url"] is None
    assert (tmp_path / "scenario-3-ui.json").exists()


@pytest.mark.asyncio
async def test_executor_runs_diagnostics_on_failure_and_preserves_primary_error(tmp_path):
    store = ScenarioStore(tmp_path / "summary.json")

    async def fail(_context):
        raise RuntimeError("provider failed")

    async def collect(_scenario_id, scenario):
        return await collect_ui_diagnostics(
            ScreenshotFailurePage(), tmp_path, scenario_id="failed-scenario",
            original_error={"error_category": scenario["error_category"]},
        )

    item = AcceptanceScenario(
        ScenarioRecord(
            acceptance_id="acceptance", scenario_id="failed-scenario", timeout_seconds=1,
            request_budget=1, expected_terminal="completed",
        ),
        fail,
    )
    result = (await execute_scenarios(
        [item], store, max_runs=1, max_requests=1, diagnostic_collector=collect,
    ))["failed-scenario"]

    assert result["status"] == "failed"
    assert result["error_category"] == "RuntimeError"
    assert result["actual_observations"]["diagnostic_errors"][0]["source"] == "screenshot"
    assert result["diagnostic_files"]["ui_snapshot"].endswith("failed-scenario-ui.json")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["timeout", "cancel"])
async def test_executor_collects_diagnostics_for_timeout_and_cancellation(tmp_path, outcome):
    store = ScenarioStore(tmp_path / f"{outcome}-summary.json")
    collected = []

    async def wait_forever(_context):
        await asyncio.Event().wait()

    async def collect(scenario_id, scenario):
        collected.append((scenario_id, scenario["status"]))
        return await collect_ui_diagnostics(
            FakePage(), tmp_path, scenario_id=scenario_id,
            visible_run_status=scenario["status"],
        )

    record = ScenarioRecord(
        acceptance_id="acceptance", scenario_id=outcome, timeout_seconds=0.01,
        request_budget=1, expected_terminal="completed",
    )
    execution = asyncio.create_task(execute_scenarios(
        [AcceptanceScenario(record, wait_forever)], store,
        max_runs=1, max_requests=1, diagnostic_collector=collect,
    ))
    if outcome == "cancel":
        while outcome not in store.scenario_ids:
            await asyncio.sleep(0)
        while store.get(outcome)["status"] != "running":
            await asyncio.sleep(0)
        execution.cancel()
    result = (await execution)[outcome]
    expected = "cancelled" if outcome == "cancel" else "timed_out"
    assert result["status"] == expected
    assert collected == [(outcome, expected)]
    assert result["actual_observations"]["ui"]["visible_run_status"] == expected