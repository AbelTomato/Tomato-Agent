"""Bounded, fail-fast execution for real Provider acceptance scenarios."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

try:
    from tests.fixtures.provider_acceptance_state import AcceptanceStateError, ScenarioRecord, ScenarioStore
except ModuleNotFoundError:
    try:
        from fixtures.provider_acceptance_state import AcceptanceStateError, ScenarioRecord, ScenarioStore
    except ModuleNotFoundError:
        from provider_acceptance_state import AcceptanceStateError, ScenarioRecord, ScenarioStore


@dataclass
class ScenarioContext:
    store: ScenarioStore
    scenario_id: str
    record: ScenarioRecord

    def consume_request(self) -> int:
        return self.store.consume_request(self.scenario_id)


@dataclass
class AcceptanceScenario:
    record: ScenarioRecord
    run: Callable[[ScenarioContext], Awaitable[dict[str, Any]]]


def _matches(record: ScenarioRecord, result: dict[str, Any]) -> bool:
    return (
        result.get("terminal") == record.expected_terminal
        and set(record.expected_events).issubset(set(result.get("events", [])))
        and set(record.expected_operations).issubset(set(result.get("operations", [])))
    )


def count_recorded_calls(path: str | Path, scenario_id: str | None = None) -> int:
    attempts_path = Path(path)
    if not attempts_path.exists():
        return 0
    count = 0
    for line in attempts_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if scenario_id is None or item.get("scenario_id") == scenario_id:
            count += 1
    return count


async def execute_scenarios(
    scenarios: list[AcceptanceScenario],
    store: ScenarioStore,
    *,
    max_runs: int,
    max_requests: int,
    prior_runs: int = 0,
    prior_requests: int = 0,
    request_observer: Callable[[str, ScenarioRecord], None] | None = None,
    diagnostic_collector: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    calls = prior_requests
    started_runs = prior_runs + sum(
        1 for scenario_id in store.scenario_ids if store.get(scenario_id)["status"] != "pending"
    )

    for scenario in scenarios:
        record = scenario.record
        try:
            store.add(record)
        except AcceptanceStateError:
            results[record.scenario_id] = store.get(record.scenario_id)
            break
        record.diagnostic_files.setdefault("summary", str(store.path))
        store.update(record.scenario_id, diagnostic_files=record.diagnostic_files)
        if started_runs >= max_runs:
            store.transition(record.scenario_id, "failed", failed_phase="budget",
                             error_category="global_run_budget_exhausted")
            results[record.scenario_id] = store.get(record.scenario_id)
            break
        if calls >= max_requests:
            store.transition(record.scenario_id, "failed", failed_phase="budget",
                             error_category="global_request_budget_exhausted")
            results[record.scenario_id] = store.get(record.scenario_id)
            break

        store.transition(record.scenario_id, "running")
        started_runs += 1
        try:
            if request_observer is not None:
                request_observer(record.scenario_id, record)
            result = await asyncio.wait_for(
                scenario.run(ScenarioContext(store=store, scenario_id=record.scenario_id, record=record)),
                timeout=record.timeout_seconds,
            )
            request_count = max(0, int(result.pop("request_count", 0)))
            recorded_count = min(request_count, record.request_budget)
            store._record(record.scenario_id).request_count = recorded_count
            store._write()
            calls += recorded_count
            if request_count > record.request_budget:
                raise AcceptanceStateError("request_budget_exhausted")
            status = "passed" if _matches(record, result) else "failed"
            category = None if status == "passed" else "expectation_mismatch"
            phase = None if status == "passed" else "assertions"
            if status == "failed":
                result["assertion_failure"] = "expectation_mismatch"
                budget_failure = any(
                    (failure.get("diagnostic") or {}).get("reason_code") == "provider_call_budget_exhausted"
                    for failure in result.get("execution_failures", [])
                )
                if result.get("budget_blocks") or budget_failure:
                    category, phase = "provider_call_budget_exhausted", "budget"
                elif result.get("execution_failures"):
                    category, phase = "execution_failed", "execution"
            store.transition(record.scenario_id, status,
                             run_id=result.get("run_id"), actual_observations=result,
                             failed_phase=phase,
                             error_category=category)
        except asyncio.TimeoutError:
            calls += min(record.request_count, record.request_budget)
            store.transition(record.scenario_id, "timed_out", failed_phase="scenario",
                             actual_observations={"request_count": record.request_count},
                             error_category="scenario_deadline_exceeded")
        except asyncio.CancelledError:
            calls += min(record.request_count, record.request_budget)
            store.transition(record.scenario_id, "cancelled", failed_phase="scenario",
                             actual_observations={"request_count": record.request_count},
                             error_category="cancelled")
            results[record.scenario_id] = store.get(record.scenario_id)
        except AcceptanceStateError as exc:
            request_count = record.request_count
            if str(exc) == "request_budget_exhausted":
                request_count = max(request_count, record.request_budget)
                calls += max(0, record.request_budget - record.request_count)
                store._record(record.scenario_id).request_count = request_count
                store._write()
            store.transition(record.scenario_id, "failed", failed_phase="budget",
                             actual_observations={"request_count": request_count,
                                                  "error_type": type(exc).__name__,
                                                  "error": str(exc)},
                             error_category=str(exc))
        except Exception as exc:
            store.transition(record.scenario_id, "failed", failed_phase="scenario",
                             actual_observations={"request_count": record.request_count,
                                                  "error_type": type(exc).__name__,
                                                  "error": str(exc)[:500]},
                             error_category=type(exc).__name__)

        if diagnostic_collector is not None:
            current = store.get(record.scenario_id)
            try:
                diagnostics = await diagnostic_collector(record.scenario_id, current)
                attachments = {"ui": diagnostics.get("snapshot", {})}
                if diagnostics.get("diagnostic_errors"):
                    attachments["diagnostic_errors"] = diagnostics["diagnostic_errors"]
                store.attach_diagnostics(
                    record.scenario_id,
                    actual_observations=attachments,
                    diagnostic_files=diagnostics.get("diagnostic_files") or {},
                )
            except Exception as diagnostic_error:
                store.attach_diagnostics(
                    record.scenario_id,
                    actual_observations={"diagnostic_errors": [{
                        "source": "diagnostic_collector",
                        "error_type": type(diagnostic_error).__name__,
                        "error": str(diagnostic_error)[:500],
                    }]},
                )

        results[record.scenario_id] = store.get(record.scenario_id)
        if results[record.scenario_id]["status"] != "passed":
            break
    return results