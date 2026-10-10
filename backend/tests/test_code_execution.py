"""Lease-bound code execution contracts, using independent SQLite workspaces."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.agent.code_task import CodeTaskService, CodeTaskStrategy
from app.agent.models import CodeTaskRequest, LLMResponse
from app.agent.harness_models import Budget
from app.execution.repository import ExecutionRepository
from app.execution.state_machine import StaleExecutionOwner
from test_code_tasks_api import make_app, execution_dependencies


class Clock:
    def __init__(self):
        self.value = datetime.now(timezone.utc)

    def now(self):
        return self.value


@pytest.mark.asyncio
async def test_budget_change_after_creation_is_rejected_without_claim(tmp_path):
    service = CodeTaskService.for_testing(tmp_path)
    run = await service.create(CodeTaskRequest(task="Keep frozen budget"))
    before = await service.execution_repository.get_snapshot(run.id)

    class NoModel:
        async def complete(self, messages, tools):
            pytest.fail("budget mismatch must not call the model")

    changed = Budget.model_validate({**before.budget_limits, "max_duration_seconds": 60})
    result = await service.execute(str(run.id), llm=NoModel(), strategy=CodeTaskStrategy(), budget=changed)
    assert result["error"]["code"] == "budget_mismatch"
    assert (await service.run_repository.get_run(run.id)).status == "queued"
    assert await service.execution_repository.get_snapshot(run.id) == before
    with pytest.raises(KeyError):
        await service.execution_repository.latest_attempt(run.id)


@pytest.mark.asyncio
async def test_code_task_service_has_no_legacy_execution_write_helpers(tmp_path):
    service = CodeTaskService.for_testing(tmp_path)

    assert not hasattr(service, "run_test_with_retry")
    assert not hasattr(service, "record_test_result")
    assert not hasattr(service, "record_diff")
    assert not hasattr(service, "_fail")


@pytest.mark.asyncio
async def test_missing_model_fails_run_through_execution_repository(tmp_path):
    service = CodeTaskService.for_testing(tmp_path)
    run = await service.create(CodeTaskRequest(task="Model missing"))

    result = await service.execute(str(run.id))

    assert result["error"]["code"] == "model_not_configured"
    assert (await service.execution_repository.get_run(run.id)).status == "failed"
    with pytest.raises(KeyError):
        await service.execution_repository.latest_attempt(run.id)


@pytest.mark.asyncio
async def test_create_is_queued_and_execution_finishes_atomic_snapshot(tmp_path):
    app = make_app(tmp_path)
    execution_dependencies(app)
    async with app.router.lifespan_context(app):
        service = app.state.code_task_service
        run = await service.create(CodeTaskRequest(task="Fix addition"))
        assert run.status == "queued"
        result = await service.execute(str(run.id))
        assert result["status"] == "completed"
        snapshot = await service.execution_repository.get_snapshot(run.id)
        assert snapshot.harness_state["completion_validated"] is True
        assert snapshot.test_results[-1]["workspace_diff_sha256"]
        events = await service.run_repository.list_events(run.id)
        assert snapshot.last_event_sequence == events[-1].sequence
        attempt = await service.execution_repository.latest_attempt(run.id)
        assert attempt.status == "succeeded"
        assert not service._execution_tasks
        assert not service._heartbeat_tasks


@pytest.mark.asyncio
async def test_duplicate_execution_across_services_uses_database_ownership(tmp_path):
    service = CodeTaskService.for_testing(tmp_path)
    run = await service.create(CodeTaskRequest(task="Fix addition"))
    other = CodeTaskService.for_testing(tmp_path)
    await other.initialize()
    started = asyncio.Event()

    class LLM:
        async def complete(self, messages, tools):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(service.execute(str(run.id), llm=LLM(), strategy=CodeTaskStrategy()))
    try:
        await asyncio.wait_for(started.wait(), 2)
        duplicate = await other.execute(str(run.id), llm=LLM(), strategy=CodeTaskStrategy())
        assert duplicate["error"]["code"] == "execution_claim_conflict"
        await service.cancel(str(run.id))
        result = await asyncio.wait_for(task, 2)
        assert result["status"] == "cancelled"
        assert (await other.run_repository.get_run(run.id)).status == "cancelled"
        assert not service._heartbeat_tasks
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stale_finish_cannot_publish_terminal_snapshot(tmp_path):
    app = make_app(tmp_path)
    execution_dependencies(app)
    clock = Clock()
    async with app.router.lifespan_context(app):
        service = app.state.code_task_service
        repository = service.execution_repository
        repository.clock = clock
        run = await service.create(CodeTaskRequest(task="Fix addition"))
        lease = await repository.claim(run.id, "old", lease_seconds=30)
        snapshot = await repository.get_snapshot(run.id)
        clock.value += timedelta(seconds=31)
        with pytest.raises(StaleExecutionOwner):
            await repository.finish(lease, "completed", snapshot=snapshot,
                                    event_type="code_task.completed")
        assert (await repository.get_run(run.id)).status == "running"


@pytest.mark.asyncio
async def test_recover_inspect_is_read_only_and_legacy_is_rejected(tmp_path):
    app = make_app(tmp_path)
    async with app.router.lifespan_context(app):
        service = app.state.code_task_service
        run = await service.run_repository.create_run("code_task", {"task": "old"}, "old")
        await service.run_repository.update_status(run.id, "running")
        before = await service.run_repository.get_run(run.id)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/api/code-tasks/{run.id}/recover", json={
                "expected_version": before.version, "action": "inspect"})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "legacy_snapshot"
        assert await service.run_repository.get_run(run.id) == before


@pytest.mark.asyncio
async def test_ready_finish_recovery_reuses_response_and_validates_completion(tmp_path):
    app = make_app(tmp_path)
    execution_dependencies(app)
    clock = Clock()
    async with app.router.lifespan_context(app):
        service = app.state.code_task_service
        repository = service.execution_repository
        repository.clock = clock
        run = await service.create(CodeTaskRequest(task="Fix addition"))
        original = service.validate_completion

        class Crash(BaseException):
            pass

        async def crash(*args, **kwargs):
            raise Crash

        service.validate_completion = crash
        with pytest.raises(Crash):
            await service.execute(str(run.id))
        service.validate_completion = original
        before = await service.run_repository.get_run(run.id)
        clock.value += timedelta(seconds=31)
        events = await service.run_repository.list_events(run.id)
        inspected = await service.recover(str(run.id), expected_version=before.version, action="inspect")
        assert inspected["recovery_reason"] == "snapshot_resumable"
        assert await service.run_repository.list_events(run.id) == events

        class NoModel:
            async def complete(self, messages, tools):
                pytest.fail("persisted final must not call the model")

        service.llm = NoModel()
        result = await service.recover(str(run.id), expected_version=before.version, action="continue")
        assert result["status"] == "completed"
        assert (await repository.latest_attempt(run.id)).attempt_no == 2
        assert not service._heartbeat_tasks


@pytest.mark.asyncio
async def test_heartbeat_failure_cancels_external_call_and_reaps_tasks(tmp_path):
    service = CodeTaskService.for_testing(tmp_path)
    run = await service.create(CodeTaskRequest(task="Fix addition"))
    service.heartbeat_seconds = 0.01
    started = asyncio.Event()
    cleaned = asyncio.Event()

    class LLM:
        async def complete(self, messages, tools):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

    async def lost(*args, **kwargs):
        await started.wait()
        raise StaleExecutionOwner("lost")

    service.execution_repository.heartbeat = lost
    result = await asyncio.wait_for(service.execute(str(run.id), llm=LLM(), strategy=CodeTaskStrategy()), 2)
    assert result["error"]["code"] == "stale_execution_owner"
    assert cleaned.is_set()
    assert (await service.run_repository.get_run(run.id)).status == "running"
    assert not service._execution_tasks
    assert not service._heartbeat_tasks

@pytest.mark.asyncio
async def test_persistent_diff_is_committed_only_by_execution_kernel(tmp_path):
    app = make_app(tmp_path)
    execution_dependencies(app)
    async with app.router.lifespan_context(app):
        service = app.state.code_task_service
        run = await service.create(CodeTaskRequest(task="Fix addition"))
        result = await service.execute(str(run.id))
        assert result["status"] == "completed"
        events = await service.run_repository.list_events(run.id)
        assert not any(event.event_type == "code_task.diff_created" for event in events)
        snapshot = await service.execution_repository.get_snapshot(run.id)
        assert snapshot.harness_state["diff_artifact_id"] in snapshot.artifact_ids
        assert snapshot.last_event_sequence == events[-1].sequence
