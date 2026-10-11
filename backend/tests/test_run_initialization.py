import asyncio
from pathlib import Path
import sys

import aiosqlite
import pytest

from app.agent.code_task import CodeTaskService
from app.agent.models import CodeTaskRequest
from test_original_code_task import environment
from test_task_dispatch import request, setup


@pytest.mark.parametrize("session_task", [False, True])
async def test_code_run_is_not_claimable_before_initial_snapshot(tmp_path, monkeypatch, session_task):
    if session_task:
        service, session, workspace, _, _ = await environment(tmp_path)
    else:
        service = CodeTaskService.for_testing(tmp_path)
    execute = aiosqlite.Connection.execute
    observations = []
    claims = []

    async def interleave(db, sql, parameters=None):
        if sql.lstrip().upper().startswith("INSERT INTO TASK_RUN_CHECKPOINTS"):
            async with aiosqlite.connect(service.run_repository.path) as reader:
                observations.append(await (await execute(reader,
                    "SELECT id FROM task_runs WHERE status='queued'")).fetchall())
            claims.append(await service.execution_repository.claim_next(
                "early-worker", lease_seconds=30))
        return await execute(db, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute", interleave)
    if session_task:
        run = await service.create_for_session(session, CodeTaskRequest(task="fix"),
            workspace.workspace_id, "first")
    else:
        run = await service.create(CodeTaskRequest(task="fix"))
    assert observations == [[]]
    assert claims == [None]
    claimed, _ = await service.execution_repository.claim_next("ready-worker", lease_seconds=30)
    assert claimed.id == run.id
    snapshot = await service.execution_repository.get_snapshot(run.id)
    assert snapshot.phase == "ready_model"
    events = await service.run_repository.list_events(run.id)
    assert sum(event.event_type == "code_task.created" for event in events) == 1


async def test_creation_event_failure_rolls_back_run_and_snapshot(tmp_path, monkeypatch):
    service, session, workspace, _, _ = await environment(tmp_path)
    execute = aiosqlite.Connection.execute

    async def fail_event(db, sql, parameters=None):
        if sql.lstrip().upper().startswith("INSERT INTO TASK_RUN_EVENTS"):
            raise RuntimeError("creation event failed")
        return await execute(db, sql, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(aiosqlite.Connection, "execute", fail_event)
        with pytest.raises(RuntimeError, match="creation event failed"):
            await service.create_for_session(session, CodeTaskRequest(task="fix"),
                workspace.workspace_id, "first")
    assert await service.run_repository.list_for_session(session) == []
    async with aiosqlite.connect(service.run_repository.path) as db:
        assert await (await db.execute("SELECT run_id FROM task_run_checkpoints")).fetchall() == []
    run = await service.create_for_session(session, CodeTaskRequest(task="fix"),
        workspace.workspace_id, "first")
    before = await service.run_repository.get_checkpoint(run.id)
    replay = await service.create_for_session(session, CodeTaskRequest(task="fix"),
        workspace.workspace_id, "first")
    assert replay.id == run.id
    assert await service.run_repository.get_checkpoint(run.id) == before
    assert len(await service.run_repository.list_events(run.id)) == 1


@pytest.mark.parametrize("kind", ["chat", "knowledge", "writing"])
async def test_stage_hard_exit_before_snapshot_does_not_leave_busy_session(tmp_path, kind):
    deps, session = await setup(tmp_path)
    script = '''
import asyncio, os, sys
from pathlib import Path
from uuid import UUID
import aiosqlite
from app.execution.repository import ExecutionRepository
from app.runs.repository import RunRepository
from app.sessions.repository import SessionRepository
from app.tasks.models import UnifiedTaskRequest
from app.tasks.service import TaskService
execute = aiosqlite.Connection.execute
async def crash(db, sql, parameters=None):
    if sql.lstrip().upper().startswith("INSERT INTO TASK_RUN_CHECKPOINTS"):
        os._exit(73)
    return await execute(db, sql, parameters)
async def main():
    path = Path(sys.argv[1])
    service = TaskService(RunRepository(path), ExecutionRepository(path),
        SessionRepository(path), {}, enabled=True, config_digest="test")
    aiosqlite.Connection.execute = crash
    await service.submit(UUID(sys.argv[2]), UnifiedTaskRequest(
        kind=sys.argv[3], message="hello", client_request_id="crashed"))
asyncio.run(main())
'''
    child = await asyncio.create_subprocess_exec(sys.executable, "-c", script,
        str(deps.run_repository.path), str(session), kind,
        cwd=Path(__file__).parents[1], stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        _, stderr = await asyncio.wait_for(child.communicate(), 10)
        assert child.returncode == 73, stderr.decode()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    await deps.execution_worker.recover_on_startup()
    assert await deps.run_repository.list_for_session(session) == []
    run = await deps.task_service.submit(session, request(kind))
    claimed, _ = await deps.execution_repository.claim_next("restarted", lease_seconds=30)
    assert claimed.id == run.id