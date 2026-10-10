import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest

from app.application import create_app
from app.agent.models import CodeTaskRequest
from app.execution.worker import ExecutionWorker
from app.execution.models import OperationInput
from app.settings import Settings
from test_code_tasks_api import execution_dependencies


def make_app(tmp_path, **overrides):
    return create_app(Settings(
        _env_file=None, database_path=tmp_path / "agent.db", docs_root=tmp_path,
        draft_directory=tmp_path / "drafts",
        code_task_workspace_root=tmp_path / "workspaces",
        code_task_artifact_root=tmp_path / "artifacts", llm_api_key="",
        knowledge_pipeline_enabled=False, knowledge_rerank_enabled=False,
        **overrides,
    ))


@pytest.mark.asyncio
async def test_app_lifespan_initializes_recovers_and_starts_worker(tmp_path, monkeypatch):
    app = make_app(tmp_path, code_task_worker_enabled=True)
    calls = []
    service = app.state.code_task_service
    initialize = service.initialize

    async def init():
        await initialize()
        calls.append("init")

    async def recover(worker):
        assert worker.repository is service.execution_repository
        calls.append("recover")
        return []

    async def start(worker):
        calls.append("start")

    async def stop(worker):
        calls.append("stop")

    monkeypatch.setattr(service, "initialize", init)
    monkeypatch.setattr(ExecutionWorker, "recover_on_startup", recover)
    monkeypatch.setattr(ExecutionWorker, "start", start)
    monkeypatch.setattr(ExecutionWorker, "stop", stop)
    async with app.router.lifespan_context(app):
        assert app.state.execution_worker is app.state.dependencies.execution_worker
        assert calls == ["init", "recover", "start"]
    assert calls == ["init", "recover", "start", "stop"]


@pytest.mark.parametrize("value", [0, 2])
def test_invalid_worker_concurrency_is_rejected(value):
    with pytest.raises(ValueError, match="max_concurrency"):
        Settings(_env_file=None, code_task_worker_max_concurrency=value)


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_invalid_poll_interval_is_rejected(value):
    with pytest.raises(ValueError, match="poll_interval"):
        Settings(_env_file=None, code_task_worker_poll_interval_seconds=value)


@pytest.mark.asyncio
async def test_disabled_worker_is_explicitly_reported(tmp_path):
    app = make_app(tmp_path, code_task_worker_enabled=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/code-tasks", json={"task": "Fix addition"})
            assert created.status_code == 202
            assert created.json()["dispatch_status"] == "worker_disabled"
            queried = await client.get(f"/api/code-tasks/{created.json()['run_id']}")
            assert queried.json()["dispatch_status"] == "worker_disabled"
            assert queried.json()["status"] == "queued"
        assert app.state.execution_worker._loop_task is None


@pytest.mark.asyncio
async def test_enabled_worker_executes_with_single_claim_and_cleans_handles(tmp_path):
    app = make_app(tmp_path, code_task_worker_enabled=True,
                   code_task_worker_poll_interval_seconds=0.01)
    execution_dependencies(app)
    async with app.router.lifespan_context(app):
        service = app.state.code_task_service
        run = await service.create(CodeTaskRequest(task="Fix addition"))
        async with asyncio.timeout(3):
            while (await service.run_repository.get_run(run.id)).status == "queued":
                await asyncio.sleep(0.01)
            while (await service.run_repository.get_run(run.id)).status == "running":
                await asyncio.sleep(0.01)
        current = await service.run_repository.get_run(run.id)
        assert current.status == "completed", current.error
        attempt = await service.execution_repository.latest_attempt(run.id)
        assert attempt.fencing_token == 1
        assert attempt.status == "succeeded"
    assert app.state.execution_worker._loop_task is None
    assert not app.state.execution_worker._active
    assert not service._execution_tasks
    assert not service._heartbeat_tasks


@pytest.mark.asyncio
async def test_startup_recovers_expired_run_with_current_policy(tmp_path):
    app = make_app(tmp_path, code_task_worker_enabled=True)
    service = app.state.code_task_service
    await service.initialize()
    run = await service.create(CodeTaskRequest(task="Fix addition"))
    repository = service.execution_repository

    class Clock:
        value = datetime.now(timezone.utc)

        def now(self):
            return self.value

    clock = Clock()
    repository.clock = clock
    await repository.claim(run.id, "old", lease_seconds=30)
    clock.value += timedelta(seconds=31)
    # A changed live policy must not be mistaken for the snapshot policy.
    app.state.config.code_task_tool_timeout_seconds = 19
    async with app.router.lifespan_context(app):
        current = await repository.get_run(run.id)
        assert current.status == "running"
        attempt = await repository.latest_attempt(run.id)
        assert attempt.status == "expired"
        assert app.state.execution_worker._active == {}
        assert (await repository.get_recovery_candidate(run.id)).snapshot is not None


@pytest.mark.asyncio
async def test_startup_requeues_safe_run_and_waits_unknown_file_write(tmp_path):
    app = make_app(tmp_path, code_task_worker_enabled=True)
    service = app.state.code_task_service
    await service.initialize()
    safe = await service.create(CodeTaskRequest(task="Safe restart"))
    unknown = await service.create(CodeTaskRequest(task="Unknown write"))
    repository = service.execution_repository

    class Clock:
        value = datetime.now(timezone.utc)

        def now(self):
            return self.value

    clock = Clock()
    repository.clock = clock
    await repository.claim(safe.id, "safe-old", lease_seconds=30)
    lease = await repository.claim(unknown.id, "unknown-old", lease_seconds=30)
    snapshot = await repository.get_snapshot(unknown.id)
    payload = {"path": "calculator.py", "content": "unknown"}
    operation = OperationInput(
        id=uuid4(),
        logical_index=0, kind="tool", tool_name="write_file", recovery_class="file_write",
        input_payload=payload,
        before_digests={"calculator.py": "before"}, after_digests={"calculator.py": "after"},
    )
    snapshot = snapshot.model_copy(update={"pending_operation_id": operation.id})
    await repository.prepare_operation(lease, operation, snapshot)
    await repository.start_step(lease, operation.id, snapshot)
    clock.value += timedelta(seconds=31)
    async with app.router.lifespan_context(app):
        # start() only schedules the loop; recovery is complete before its first claim.
        assert (await repository.get_run(unknown.id)).status == "waiting"
        events = await service.run_repository.list_events(safe.id)
        assert any(event.event_type == "execution.recovered" for event in events)


@pytest.mark.asyncio
async def test_worker_without_model_fails_claimed_run_explicitly(tmp_path):
    app = make_app(tmp_path, code_task_worker_enabled=True,
                   code_task_worker_poll_interval_seconds=0.01)
    async with app.router.lifespan_context(app):
        service = app.state.code_task_service
        run = await service.create(CodeTaskRequest(task="No model"))
        async with asyncio.timeout(3):
            while (await service.run_repository.get_run(run.id)).status in {"queued", "running"}:
                await asyncio.sleep(0.01)
        current = await service.run_repository.get_run(run.id)
        assert current.status == "failed"
        assert current.error["code"] == "model_not_configured"