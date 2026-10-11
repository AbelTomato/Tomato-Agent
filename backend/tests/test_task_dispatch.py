import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.execution.state_machine import StaleExecutionOwner
from app.settings import Settings
from app.tasks.models import UnifiedTaskRequest, TaskActionRequest
from test_application_lifecycle import make_app


async def setup(tmp_path, **options):
    app = make_app(tmp_path, task_worker_enabled=True, **options)
    deps = app.state.dependencies
    deps.task_service.model_available = True
    await deps.session_repository.init()
    await deps.run_repository.init()
    await deps.code_task_service.initialize()
    await deps.writing_repository.init()
    await deps.writing_execution_repository.init()
    session = await deps.session_repository.create_session()
    return deps, session


def request(kind="chat", **values):
    return UnifiedTaskRequest(kind=kind, message="hello", client_request_id=str(uuid4()), **values)


@pytest.mark.parametrize("new,old,expected", [(None, False, False), (None, True, True),
                                               (False, True, False), (True, False, True)])
def test_worker_flag_precedence(new, old, expected):
    config = Settings(_env_file=None, task_worker_enabled=new, code_task_worker_enabled=old)
    assert config.effective_task_worker_enabled is expected


@pytest.mark.parametrize("kind", ["chat", "knowledge", "writing"])
async def test_stage_dispatch_without_workspace_and_request_replay(tmp_path, kind):
    deps, session = await setup(tmp_path)
    seen = []

    class Adapter:
        async def execute(self, run, lease, port):
            seen.append((run.task_type, port.workspace_id, lease))
            await port.finish("completed", result={"answer": "done"})

    deps.task_service.adapters[kind] = Adapter()
    req = request(kind)
    run = await deps.task_service.submit(session, req)
    assert (await deps.task_service.submit(session, req)).id == run.id
    assert run.workspace_id is None
    assert await deps.execution_worker.run_once()
    assert seen[0][0:2] == (kind, None)
    current = await deps.run_repository.get_run(run.id)
    assert current.status == "completed" and current.result == {"answer": "done"}
    assert (await deps.execution_repository.latest_attempt(run.id)).fencing_token == 1


async def test_disabled_worker_rejects_submit_without_creating_run(tmp_path):
    deps, session = await setup(tmp_path)
    deps.task_service.enabled = False
    with pytest.raises(RuntimeError, match="worker_disabled"):
        await deps.task_service.submit(session, request())
    assert await deps.run_repository.list_for_session(session) == []


async def test_session_conflict_and_request_content_conflict(tmp_path):
    deps, session = await setup(tmp_path)
    req = request()
    await deps.task_service.submit(session, req)
    with pytest.raises(RuntimeError, match="session_busy"):
        await deps.task_service.submit(session, request("writing"))
    with pytest.raises(RuntimeError, match="request_conflict"):
        await deps.task_service.submit(session, req.model_copy(update={"message": "changed"}))


async def test_exception_is_failed_and_late_result_is_fenced(tmp_path):
    deps, session = await setup(tmp_path)

    class Broken:
        async def execute(self, run, lease, port):
            raise ValueError("private failure")

    deps.task_service.adapters["chat"] = Broken()
    run = await deps.task_service.submit(session, request())
    await deps.execution_worker.run_once()
    assert (await deps.run_repository.get_run(run.id)).error == {"code": "worker_executor_failed"}
    started, release = asyncio.Event(), asyncio.Event()
    rejected = []

    class Late:
        async def execute(self, run, lease, port):
            started.set()
            await release.wait()
            try:
                await port.finish("completed", result={"answer": "late"})
            except StaleExecutionOwner:
                rejected.append(True)
                raise

    deps.task_service.adapters["chat"] = Late()
    run = await deps.task_service.submit(session, request())
    execution = asyncio.create_task(deps.execution_worker.run_once())
    await started.wait()
    current = await deps.run_repository.get_run(run.id)
    await deps.task_service.act(run.id, TaskActionRequest(action="cancel", action_id="cancel",
        expected_version=current.version, client_request_id="cancel"))
    release.set()
    await execution
    current = await deps.run_repository.get_run(run.id)
    assert rejected and current.status == "cancelled" and current.result is None


async def test_stage_crash_interrupted_does_not_replay_generation(tmp_path):
    deps, session = await setup(tmp_path)
    run = await deps.task_service.submit(session, request())
    repo = deps.execution_repository

    class Clock:
        value = datetime.now(timezone.utc)
        def now(self):
            return self.value

    clock = Clock()
    repo.clock = clock
    lease = await repo.claim(run.id, "dead", lease_seconds=1)
    snapshot = await repo.get_snapshot(run.id)
    await repo.save_snapshot(lease, snapshot.model_copy(update={"phase": "generating"}))
    clock.value += timedelta(seconds=2)
    results = await deps.execution_worker.recover_on_startup()
    assert results[0].reason_code == "interrupted"
    current = await repo.get_run(run.id)
    assert current.status == "failed" and current.error == {"code": "interrupted"}
    assert not await deps.execution_worker.run_once()


def test_options_and_workspace_are_kind_specific():
    with pytest.raises(ValueError):
        request("chat", options={"retrieval_mode": "keyword"})
    with pytest.raises(ValueError):
        request("writing", workspace_id="project")
    with pytest.raises(ValueError):
        request("code")


async def test_unconfigured_model_rejects_before_run_creation(tmp_path):
    deps, session = await setup(tmp_path)
    deps.task_service.model_available = False
    with pytest.raises(RuntimeError, match="model_unconfigured"):
        await deps.task_service.submit(session, request())
    assert await deps.run_repository.list_for_session(session) == []


async def test_code_dispatch_reuses_lease_and_blocks_other_session(tmp_path):
    from test_original_code_task import environment, Model, call
    from app.agent.models import LLMResponse
    from app.tasks.adapters.code import CodeAdapter
    deps, _ = await setup(tmp_path)
    (tmp_path / "code").mkdir()
    service, session, workspace, project, sandbox = await environment(tmp_path / "code")
    await service.initialize()
    deps.task_service.runs = service.run_repository
    deps.task_service.execution = service.execution_repository
    from app.sessions.repository import SessionRepository
    sessions = SessionRepository(service.run_repository.path)
    deps.task_service.sessions = sessions
    deps.task_service.adapters["code"] = CodeAdapter(service)
    deps.execution_worker.repository = service.execution_repository
    service.llm = Model([call("read_file", {"path": "app.py"}),
                         call("write_file", {"path": "app.py", "content": "value = 1\n"}),
                         call("run_tests", {"target": "unit"}), call("get_diff", {}),
                         LLMResponse(kind="final", content="done")])
    from app.agent.code_task import CodeTaskStrategy
    service.strategy = CodeTaskStrategy()
    run = await deps.task_service.submit(session, request("code", workspace_id=workspace.workspace_id))
    other = await sessions.create_session()
    with pytest.raises(RuntimeError, match="workspace_busy"):
        await deps.task_service.submit(other, request("code", workspace_id=workspace.workspace_id))
    await deps.execution_worker.run_once()
    current = await service.run_repository.get_run(run.id)
    assert current.status == "completed", current.error
    assert (await service.execution_repository.latest_attempt(run.id)).fencing_token == 1


async def test_cancelled_unknown_code_operation_blocks_workspace(tmp_path):
    from app.execution.models import OperationInput
    deps, session = await setup(tmp_path)
    run = await deps.run_repository.create_session_run(session, "code", {}, "project", "first")
    lease = await deps.execution_repository.claim(run.id, "old", lease_seconds=30)
    # The unresolved operation is the persisted proof of a possibly late side effect.
    from test_execution_recovery import snapshot as make_snapshot
    snapshot = make_snapshot(run.id)
    operation = OperationInput(id=uuid4(), logical_index=0, kind="tool", tool_name="write_file",
                              input_payload={"path": "app.py"}, recovery_class="file_write")
    snapshot = snapshot.model_copy(update={"pending_operation_id": operation.id})
    await deps.execution_repository.prepare_operation(lease, operation, snapshot)
    await deps.execution_repository.start_step(lease, operation.id, snapshot)
    await deps.execution_repository.request_cancel(run.id)
    other = await deps.session_repository.create_session()
    with pytest.raises(RuntimeError, match="workspace_busy"):
        await deps.run_repository.create_session_run(other, "code", {}, "project", "second")


async def test_cancel_transaction_rechecks_expected_version(tmp_path):
    deps, session = await setup(tmp_path)
    run = await deps.task_service.submit(session, request())
    await deps.execution_repository.claim(run.id, "owner", lease_seconds=30)
    with pytest.raises(RuntimeError, match="version_conflict"):
        await deps.execution_repository.request_cancel(run.id, expected_version=run.version)
    assert (await deps.run_repository.get_run(run.id)).status == "running"


async def test_stage_hard_exit_reopens_as_interrupted(tmp_path):
    import sys
    deps, session = await setup(tmp_path)
    run = await deps.task_service.submit(session, request())
    script = '''
import asyncio, os, sys
from uuid import UUID
from app.execution.repository import ExecutionRepository
from pathlib import Path
async def main():
    repo = ExecutionRepository(Path(sys.argv[1]))
    ident = UUID(sys.argv[2])
    lease = await repo.claim(ident, "hard-exit", lease_seconds=1)
    snapshot = await repo.get_snapshot(ident)
    await repo.save_snapshot(lease, snapshot.model_copy(update={"phase": "generating"}))
    os._exit(73)
asyncio.run(main())
'''
    child = await asyncio.create_subprocess_exec(sys.executable, "-c", script,
        str(deps.execution_repository.path), str(run.id), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        _, stderr = await asyncio.wait_for(child.communicate(), 10)
        assert child.returncode == 73, stderr.decode()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    class Clock:
        def now(self):
            return datetime.now(timezone.utc) + timedelta(seconds=2)
    deps.execution_repository.clock = Clock()
    await deps.execution_worker.recover_on_startup()
    current = await deps.run_repository.get_run(run.id)
    assert current.status == "failed" and current.error == {"code": "interrupted"}


@pytest.mark.parametrize("new,old,started", [(True, False, True), (False, True, False)])
async def test_lifespan_uses_single_worker_with_explicit_flag(tmp_path, monkeypatch, new, old, started):
    from app.execution.worker import ExecutionWorker
    app = make_app(tmp_path, task_worker_enabled=new, code_task_worker_enabled=old)
    calls = []
    async def start(worker):
        calls.append(worker)
    monkeypatch.setattr(ExecutionWorker, "start", start)
    async with app.router.lifespan_context(app):
        assert bool(calls) is started
        assert len(calls) <= 1


async def test_recovery_config_change_is_not_replayed(tmp_path):
    deps, session = await setup(tmp_path)
    run = await deps.task_service.submit(session, request())
    repo = deps.execution_repository
    lease = await repo.claim(run.id, "old", lease_seconds=1)
    class Clock:
        def now(self):
            return lease.lease_expires_at + timedelta(seconds=1)
    repo.clock = Clock()
    deps.task_service.config_digest = "changed"
    await deps.execution_worker.recover_on_startup()
    current = await repo.get_run(run.id)
    assert current.status == "failed" and current.error == {"code": "config_changed"}