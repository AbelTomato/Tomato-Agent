from datetime import datetime, timedelta, timezone
from uuid import UUID

import aiosqlite
import pytest

from app.execution.repository import (
    ExecutionRepository,
    StaleExecutionOwner,
)
from app.execution.models import LeaseHandle
from app.runs.repository import RunRepository
from app.runs.models import RunRepositoryConflict
from app.tasks.models import TaskActionRequest


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 10, 1, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


async def create_run(path, run_id=None):
    runs = RunRepository(path)
    await runs.init()
    if run_id is None:
        return runs, await runs.create_run("code_task", {"task": "x"}, "workspace")
    return runs, await runs.get_run(run_id)


@pytest.mark.asyncio
async def test_init_is_idempotent_and_claim_increments_run_token(tmp_path):
    path = tmp_path / "execution.db"
    runs, run = await create_run(path)
    repository = ExecutionRepository(path, clock=FakeClock())
    await repository.init()
    await repository.init()

    first = await repository.claim(run.id, "worker-1", lease_seconds=30)
    second = await repository.release(first)
    third = await repository.claim(run.id, "worker-2", lease_seconds=30)

    assert first.fencing_token == 1
    assert second.status == "released"
    assert third.fencing_token == 2
    assert third.attempt_id != first.attempt_id


@pytest.mark.asyncio
async def test_claim_competition_allows_only_one_active_owner(tmp_path):
    path = tmp_path / "execution.db"
    _, run = await create_run(path)
    repository = ExecutionRepository(path, clock=FakeClock())
    await repository.init()

    first = await repository.claim(run.id, "worker-1", lease_seconds=30)
    with pytest.raises(RuntimeError, match="already claimed"):
        await repository.claim(run.id, "worker-2", lease_seconds=30)

    assert (await repository.get_attempt(first.attempt_id)).owner_id == "worker-1"


@pytest.mark.asyncio
async def test_expired_lease_rejects_heartbeat_and_old_commit(tmp_path):
    path = tmp_path / "execution.db"
    _, run = await create_run(path)
    clock = FakeClock()
    repository = ExecutionRepository(path, clock=clock)
    await repository.init()
    lease = await repository.claim(run.id, "worker-1", lease_seconds=30)
    clock.advance(31)

    with pytest.raises(StaleExecutionOwner):
        await repository.heartbeat(lease, lease_seconds=30)
    with pytest.raises(StaleExecutionOwner):
        await repository.commit_checkpoint(lease, {"phase": "ready_model"})


@pytest.mark.asyncio
async def test_request_cancel_wins_against_finish_and_invalidates_lease(tmp_path):
    path = tmp_path / "execution.db"
    _, run = await create_run(path)
    repository = ExecutionRepository(path, clock=FakeClock())
    await repository.init()
    lease = await repository.claim(run.id, "worker-1", lease_seconds=30)

    cancelled = await repository.request_cancel(run.id)
    assert cancelled.status == "cancelled"

    with pytest.raises(StaleExecutionOwner):
        await repository.finish(lease, "completed")
    assert (await repository.get_run(run.id)).status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_action_is_atomic_and_replays_after_restart(tmp_path, monkeypatch):
    path = tmp_path / "execution.db"
    runs, run = await create_run(path)
    repository = ExecutionRepository(path, clock=FakeClock())
    await repository.init()
    lease = await repository.claim(run.id, "worker-1", lease_seconds=30)
    run = await repository.get_run(run.id)
    request = TaskActionRequest(action_id="cancel", action="cancel", expected_version=run.version,
                                client_request_id="cancel-1")
    commit = repository._commit

    async def fail_commit(db):
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(repository, "_commit", fail_commit)
    with pytest.raises(RuntimeError, match="injected commit failure"):
        await repository.request_cancel(run.id, expected_version=run.version, request=request)
    assert await repository.get_run(run.id) == run
    assert (await repository.get_attempt(lease.attempt_id)).status == "running"
    async with aiosqlite.connect(path) as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM task_actions")).fetchone())[0] == 0

    monkeypatch.setattr(repository, "_commit", commit)
    cancelled = await repository.request_cancel(run.id, expected_version=run.version, request=request)
    assert cancelled.status == "cancelled"
    events = await runs.list_events(run.id)
    restarted = ExecutionRepository(path, clock=FakeClock())
    await restarted.init()
    assert await restarted.request_cancel(run.id, expected_version=run.version, request=request) == cancelled
    with pytest.raises(RunRepositoryConflict, match="action_request_conflict"):
        await restarted.request_cancel(run.id, expected_version=run.version,
            request=request.model_copy(update={"action_id": "changed"}))
    assert await runs.list_events(run.id) == events
    with pytest.raises(StaleExecutionOwner):
        await restarted.finish(lease, "completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [False, True])
async def test_concurrent_cancel_actions_commit_once(tmp_path, terminal):
    import asyncio

    path = tmp_path / "execution.db"
    runs, run = await create_run(path)
    repository = ExecutionRepository(path, clock=FakeClock())
    await repository.init()
    if terminal:
        lease = await repository.claim(run.id, "worker-1", lease_seconds=30)
        run = await repository.finish(lease, "completed")
    request = TaskActionRequest(action_id="cancel", action="cancel", expected_version=run.version,
                                client_request_id="cancel-1")
    first, replay = await asyncio.gather(
        repository.request_cancel(run.id, request=request),
        repository.request_cancel(run.id, request=request),
    )
    assert first == replay
    assert first.status == ("completed" if terminal else "cancelled")
    events = await runs.list_events(run.id)
    assert sum(event.event_type == "execution.cancelled" for event in events) == (0 if terminal else 1)
    async with aiosqlite.connect(path) as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM task_actions")).fetchone())[0] == 1


@pytest.mark.asyncio
async def test_legacy_status_update_cannot_bypass_active_execution_owner(tmp_path):
    path = tmp_path / "execution.db"
    runs, run = await create_run(path)
    repository = ExecutionRepository(path, clock=FakeClock())
    await repository.init()
    await repository.claim(run.id, "worker-1", lease_seconds=30)

    with pytest.raises(RuntimeError, match="execution owner"):
        await runs.update_status(run.id, "completed")


@pytest.mark.asyncio
async def test_failed_transaction_does_not_leave_claim_or_event(tmp_path, monkeypatch):
    path = tmp_path / "execution.db"
    _, run = await create_run(path)
    repository = ExecutionRepository(path, clock=FakeClock())
    await repository.init()

    async def fail_commit(_db):
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(repository, "_commit", fail_commit)
    with pytest.raises(RuntimeError, match="injected commit failure"):
        await repository.claim(run.id, "worker-1", lease_seconds=30)

    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            "SELECT active_attempt_id, fencing_token FROM execution_run_heads WHERE run_id = ?",
            (str(run.id),),
        )
        assert await cursor.fetchone() is None
        cursor = await db.execute(
            "SELECT COUNT(*) FROM execution_attempts WHERE run_id = ?",
            (str(run.id),),
        )
        assert (await cursor.fetchone())[0] == 0

@pytest.mark.asyncio
@pytest.mark.parametrize("write", ["status", "event", "checkpoint"])
@pytest.mark.parametrize("released", [False, True])
async def test_legacy_writes_cannot_bypass_managed_run(tmp_path, write, released):
    path = tmp_path / "execution.db"
    runs, run = await create_run(path)
    repository = ExecutionRepository(path, clock=FakeClock())
    await repository.init()
    lease = await repository.claim(run.id, "worker", lease_seconds=30)
    if released:
        await repository.release(lease)
    before_run = await runs.get_run(run.id)
    before_events = await runs.list_events(run.id)
    before_checkpoint = await runs.get_checkpoint(run.id)

    with pytest.raises(RuntimeError, match="execution owner"):
        if write == "status":
            await runs.update_status(run.id, "completed")
        elif write == "event":
            await runs.append_event(run.id, "stale.event", {})
        else:
            await runs.save_checkpoint(run.id, {"stale": True})

    assert await runs.get_run(run.id) == before_run
    assert await runs.list_events(run.id) == before_events
    assert await runs.get_checkpoint(run.id) == before_checkpoint
