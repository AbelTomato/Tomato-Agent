import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.execution.models import ExecutionSnapshot, OperationInput
from app.execution.recovery import RecoveryObservation
from app.execution.repository import ExecutionRepository
from app.runs.repository import RunRepository


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 10, tzinfo=timezone.utc)

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class ExecutorFactory:
    def __init__(self, *, block=False, fail_ids=()):
        self.started = []
        self.leases = []
        self.block = block
        self.fail_ids = set(fail_ids)
        self.started_event = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, run, lease, port):
        self.started.append(run.id)
        self.leases.append(lease)
        self.started_event.set()
        if self.block:
            await self.release.wait()
        if run.id in self.fail_ids:
            raise RuntimeError("executor failed")
        await port.finish("completed", event_type="execution.completed")


async def make_worker(tmp_path, *, count=1, block=False, fail_ids=()):
    runs = RunRepository(tmp_path / "runs.db")
    await runs.init()
    execution = ExecutionRepository(tmp_path / "runs.db", clock=Clock())
    await execution.init()
    created = [await runs.create_run("code_task", {"task": "test"}, "workspace")
               for _ in range(count)]
    factory = ExecutorFactory(block=block, fail_ids=fail_ids)
    from app.execution.worker import ExecutionWorker

    worker = ExecutionWorker(execution, factory, poll_interval_seconds=0.01)
    return worker, runs, execution, factory, created


@pytest.mark.asyncio
async def test_run_once_claims_one_queued_run_and_executes_it(tmp_path):
    worker, _, execution, factory, runs = await make_worker(tmp_path)

    assert await worker.run_once() is True

    assert factory.started == [runs[0].id]
    assert factory.leases[0].run_id == runs[0].id
    assert await execution.get_attempt(factory.leases[0].attempt_id)


@pytest.mark.asyncio
async def test_run_once_does_not_claim_second_run_while_busy(tmp_path):
    worker, _, _, factory, runs = await make_worker(tmp_path, count=2, block=True)
    first = asyncio.create_task(worker.run_once())
    await factory.started_event.wait()

    assert await worker.run_once() is False
    factory.release.set()
    assert await first is True
    assert factory.started == [runs[0].id]


@pytest.mark.asyncio
async def test_start_stop_are_idempotent_and_stop_prevents_claims(tmp_path):
    worker, _, _, factory, _ = await make_worker(tmp_path)

    await worker.start()
    await worker.start()
    await worker.stop()
    await worker.stop()

    assert factory.started == []


@pytest.mark.asyncio
async def test_worker_continues_after_executor_exception(tmp_path):
    worker, _, _, factory, runs = await make_worker(tmp_path, count=2, fail_ids=())
    factory.fail_ids.add(runs[0].id)

    assert await worker.run_once() is True
    assert await worker.run_once() is True
    assert factory.started == [runs[0].id, runs[1].id]


@pytest.mark.asyncio
async def test_startup_recovery_requeues_safe_run_and_waits_unknown_side_effect(tmp_path):
    worker, runs, execution, _, created = await make_worker(tmp_path, count=2)
    observation = RecoveryObservation(policy_digest="policy")
    for run in created:
        snapshot = ExecutionSnapshot(
            run_id=run.id, schema_version=1, checkpoint_revision=1,
            last_event_sequence=0, phase="ready_model", logical_index=0,
            messages=[], harness_state={}, budget_limits={"max_model_calls": 5},
            reserved_model_calls=0, reserved_tool_calls=0, loop_count=0,
            elapsed_upper_bound_seconds=0, policy_digest="policy",
            workspace_id="workspace", test_results=[], artifact_ids=[],
        )
        await execution.bootstrap_run(snapshot, event_type="code_task.created", event_payload={})
        lease = await execution.claim(run.id, "crashed-worker", lease_seconds=1)
        if run.id == created[1].id:
            payload = {"path": "calculator.py"}
            operation = OperationInput(
                id=uuid4(), logical_index=0, kind="tool",
                tool_name="write_file", recovery_class="file_write",
                input_payload=payload,
            )
            await execution.prepare_operation(lease, operation, snapshot.model_copy(
                update={"pending_operation_id": operation.id}))
            step = await execution.start_step(lease, operation.id, snapshot.model_copy(
                update={"pending_operation_id": operation.id}))
            assert step
        execution.clock.advance(2)

    results = await worker.recover_on_startup(observation_factory=lambda _: observation)

    assert {str(result.run_id): result.status for result in results} == {
        str(created[0].id): "queued", str(created[1].id): "waiting",
    }


@pytest.mark.asyncio
async def test_stop_waits_for_active_execution_and_does_not_claim_next(tmp_path):
    worker, _, _, factory, runs = await make_worker(tmp_path, count=2, block=True)
    await worker.start()
    await asyncio.wait_for(factory.started_event.wait(), 2)
    stopping = asyncio.create_task(worker.stop())
    await asyncio.sleep(0.02)
    assert not stopping.done()
    factory.release.set()
    await asyncio.wait_for(stopping, 2)
    assert factory.started == [runs[0].id]
    assert worker._active == {}


@pytest.mark.asyncio
async def test_background_loop_continues_after_executor_failure(tmp_path):
    worker, runs, _, factory, created = await make_worker(tmp_path, count=2)
    factory.fail_ids.add(created[0].id)
    await worker.start()
    try:
        async with asyncio.timeout(2):
            while (await runs.get_run(created[1].id)).status != "completed":
                await asyncio.sleep(0.01)
    finally:
        await worker.stop()
    assert (await runs.get_run(created[0].id)).error == {"code": "worker_executor_failed"}
    assert factory.started == [run.id for run in created]


@pytest.mark.asyncio
async def test_claim_next_excludes_non_code_tasks_and_terminal_runs(tmp_path):
    _, runs, execution, _, created = await make_worker(tmp_path)
    await runs.create_run("writing", {}, "workspace")
    lease = await execution.claim(created[0].id, "test", lease_seconds=30)
    await execution.finish(lease, "completed")
    assert await execution.claim_next("worker", lease_seconds=30) is None


@pytest.mark.asyncio
async def test_heartbeat_failure_cancels_executor_without_stale_commit(tmp_path, monkeypatch):
    worker, runs, execution, factory, created = await make_worker(tmp_path, block=True)
    worker.heartbeat_interval_seconds = 0.01

    async def heartbeat(*args, **kwargs):
        from app.execution.state_machine import StaleExecutionOwner
        raise StaleExecutionOwner("lost")

    monkeypatch.setattr(execution, "heartbeat", heartbeat)
    assert await asyncio.wait_for(worker.run_once(), 2) is True
    assert (await runs.get_run(created[0].id)).status == "running"
    assert worker._active == {}


@pytest.mark.asyncio
async def test_repository_failure_does_not_kill_background_loop(tmp_path, monkeypatch):
    worker, runs, execution, _, created = await make_worker(tmp_path)
    original = execution.claim_next
    calls = 0

    async def claim_next(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary repository failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(execution, "claim_next", claim_next)
    await worker.start()
    try:
        async with asyncio.timeout(2):
            while (await runs.get_run(created[0].id)).status != "completed":
                await asyncio.sleep(0.01)
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_two_coordinators_cannot_claim_same_run(tmp_path):
    worker, _, execution, factory, _ = await make_worker(tmp_path, block=True)
    from app.execution.worker import ExecutionWorker
    second_factory = ExecutorFactory()
    second = ExecutionWorker(execution, second_factory)
    first = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(factory.started_event.wait(), 2)
    try:
        assert await second.run_once() is False
        assert second_factory.started == []
    finally:
        factory.release.set()
        await first


@pytest.mark.asyncio
async def test_stop_before_start_also_prevents_manual_claims(tmp_path):
    worker, _, _, factory, _ = await make_worker(tmp_path)
    await worker.stop()
    assert await worker.run_once() is False
    assert factory.started == []


@pytest.mark.asyncio
async def test_worker_can_restart_after_stop(tmp_path):
    worker, runs, _, factory, created = await make_worker(tmp_path)
    await worker.start()
    await worker.stop()
    await worker.start()
    try:
        async with asyncio.timeout(2):
            while (await runs.get_run(created[0].id)).status != "completed":
                await asyncio.sleep(0.01)
    finally:
        await worker.stop()
    assert factory.started == [created[0].id]


@pytest.mark.asyncio
async def test_missing_current_recovery_policy_is_rejected_before_expiry(tmp_path):
    worker, _, execution, _, created = await make_worker(tmp_path)
    lease = await execution.claim(created[0].id, "old", lease_seconds=1)
    execution.clock.advance(2)
    with pytest.raises(ValueError, match="observation"):
        await worker.recover_on_startup()
    assert (await execution.get_attempt(lease.attempt_id)).status == "running"


@pytest.mark.asyncio
async def test_worker_rejects_non_single_concurrency(tmp_path):
    _, _, execution, factory, _ = await make_worker(tmp_path)
    from app.execution.worker import ExecutionWorker
    with pytest.raises(ValueError):
        ExecutionWorker(execution, factory, max_concurrency=2)