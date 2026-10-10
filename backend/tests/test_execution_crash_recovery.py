"""Hard-exit evidence using isolated SQLite files and pipe barriers."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
import sys

import pytest

from app.execution.recovery import RecoveryConflict, RecoveryObservation, classify_recovery
from app.execution.repository import ExecutionRepository
from app.runs.repository import RunRepository
from test_execution_recovery import Clock


WORKER = Path(__file__).parent / "fixtures" / "execution_crash_worker.py"
BACKEND = Path(__file__).parents[1]


@asynccontextmanager
async def worker(path, run_id, mode, fault="none", recovery_class="read_only"):
    assert WORKER.is_file(), "independent crash worker has not been implemented"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(BACKEND), str(BACKEND / "tests")])}
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(WORKER), str(path), str(run_id), mode, fault, recovery_class,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env, cwd=BACKEND,
    )
    try:
        yield process
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.wait(), 10)
        stderr = (await process.stderr.read()).decode()
        assert process.returncode in {0, 73, -9}, stderr


async def receive(process):
    line = await asyncio.wait_for(process.stdout.readline(), 10)
    assert line, (await process.stderr.read()).decode()
    return json.loads(line)


async def send(process, **message):
    process.stdin.write((json.dumps(message) + "\n").encode())
    await process.stdin.drain()


async def create_run(tmp_path):
    path = tmp_path / "execution.db"
    runs = RunRepository(path)
    await runs.init()
    run = await runs.create_run("code_task", {}, "workspace")
    await ExecutionRepository(path, clock=Clock()).init()
    return path, run.id


async def recover(path, run_id):
    clock = Clock()
    clock.value += timedelta(seconds=31)
    repository = ExecutionRepository(path, clock=clock)
    attempt = await repository.latest_attempt(run_id)
    candidate = await repository.expire_and_classify(attempt.id)
    decision = classify_recovery(candidate.snapshot, candidate.operation,
                                 RecoveryObservation(policy_digest=candidate.snapshot["policy_digest"]))
    result = await repository.apply_recovery(candidate, decision)
    return repository, candidate, decision, result


@pytest.mark.parametrize("fault,model_calls,tool_calls,effects", [
    ("model_before", 1, 0, 0),
    ("response_after", 2, 1, 1),
    ("effect_after", 2, 2, 2),
    ("commit_before", 2, 2, 2),
    ("commit_after", 2, 1, 1),
    ("finish_before", 2, 1, 1),
    ("finish_after", 2, 1, 1),
])
async def test_hard_exit_reopen_and_resume(tmp_path, fault, model_calls, tool_calls, effects):
    path, run_id = await create_run(tmp_path)
    async with worker(path, run_id, "run", fault) as child:
        barrier = await receive(child)
        assert barrier["fault"] == fault
        await send(child, action="exit")
        assert await asyncio.wait_for(child.wait(), 10) == 73

    reopened = ExecutionRepository(path, clock=Clock())
    before = await reopened.get_snapshot(run_id)
    events = await RunRepository(path).list_events(run_id)
    assert before.last_event_sequence == events[-1].sequence
    if fault == "model_before":
        assert before.reserved_model_calls == 1
        assert before.in_flight_started_at is not None
    if fault in {"effect_after", "commit_before"}:
        assert (await reopened.get_operation(before.pending_operation_id)).status == "running"
        assert not any(event.event_type == "step.succeeded" for event in events)
    if fault == "finish_after":
        assert (await reopened.get_run(run_id)).status == "completed"
        assert (await reopened.latest_attempt(run_id)).status == "succeeded"
        with pytest.raises(RecoveryConflict, match="terminal_run"):
            await reopened.claim(run_id, "resurrect", lease_seconds=30)
    else:
        assert (await reopened.get_run(run_id)).status == "running"
        repository, candidate, decision, result = await recover(path, run_id)
        assert decision.action in {"retry", "reuse"}
        assert result.status == "queued"
        async with worker(path, run_id, "resume") as child:
            completed = await receive(child)
            assert completed["status"] == ("failed" if fault == "model_before" else "completed")
            assert await asyncio.wait_for(child.wait(), 10) == 0
        assert (await repository.latest_attempt(run_id)).fencing_token > candidate.fencing_token

    final = await reopened.get_snapshot(run_id)
    assert final.reserved_model_calls == model_calls
    assert final.reserved_tool_calls == tool_calls
    effect_file = path.with_suffix(".effects")
    assert (len(effect_file.read_text().splitlines()) if effect_file.exists() else 0) == effects
    if fault in {"model_before", "effect_after", "commit_before"}:
        assert final.elapsed_upper_bound_seconds >= before.in_flight_max_seconds
    assert (await reopened.get_run(run_id)).status == ("failed" if fault == "model_before" else "completed")


@pytest.mark.parametrize("recovery_class", ["file_write", "sandbox"])
async def test_unknown_side_effect_waits_and_cancel_does_not_claim_cleanup(tmp_path, recovery_class):
    path, run_id = await create_run(tmp_path)
    async with worker(path, run_id, "run", "effect_after", recovery_class) as child:
        assert (await receive(child))["fault"] == "effect_after"
        await send(child, action="exit")
        assert await asyncio.wait_for(child.wait(), 10) == 73
    repository, candidate, decision, result = await recover(path, run_id)
    assert decision.reason_code == "unknown_side_effect"
    assert result.status == "waiting"
    assert (await repository.get_operation(candidate.operation.id)).status == "unknown"
    with pytest.raises(RecoveryConflict, match="manual_recovery_required"):
        await repository.claim(run_id, "unsafe-replay", lease_seconds=30)
    assert (await repository.request_cancel(run_id)).status == "cancelled"
    assert path.with_suffix(".effects").read_text().splitlines() == [recovery_class]
    assert (await repository.get_operation(candidate.operation.id)).status == "unknown"
    events = await RunRepository(path).list_events(run_id)
    assert not any("clean" in event.event_type for event in events)


async def test_two_process_claim_and_paused_owner_is_fenced(tmp_path):
    path, run_id = await create_run(tmp_path)
    async with worker(path, run_id, "claim") as first, worker(path, run_id, "claim") as second:
        assert await receive(first) == {"ready": True}
        assert await receive(second) == {"ready": True}
        await asyncio.gather(send(first, action="claim"), send(second, action="claim"))
        results = await asyncio.gather(receive(first), receive(second))
        assert sorted(result["claimed"] for result in results) == [False, True]
        winner = first if results[0]["claimed"] else second
        repository, candidate, decision, result = await recover(path, run_id)
        assert result.status == "queued"
        new = await repository.claim(run_id, "replacement", lease_seconds=30)
        assert new.fencing_token > candidate.fencing_token
        snapshot = await repository.get_snapshot(run_id)
        await send(winner, action="stale", now=repository.clock.now().isoformat())
        rejected = await receive(winner)
        assert rejected == {"rejected": ["heartbeat", "snapshot", "model", "step", "finish"]}
        assert await repository.get_snapshot(run_id) == snapshot
        assert (await repository.get_run(run_id)).status == "running"
        assert (await repository.latest_attempt(run_id)).id == new.attempt_id