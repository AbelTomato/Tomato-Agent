import json
from uuid import UUID

import aiosqlite
import pytest

from app.runs.models import RunRecord, RunRepositoryConflict, RunStatus
from app.runs.repository import RunRepository


@pytest.mark.asyncio
async def test_run_repository_persists_runs_events_and_checkpoint(tmp_path):
    repository = RunRepository(tmp_path / "runs.db")
    await repository.init()

    run = await repository.create_run(
        task_type="code_task",
        request={"task": "fix the test"},
        workspace_id="workspace-1",
    )

    assert isinstance(run.id, UUID)
    assert run.status == "queued"
    assert run.request == {"task": "fix the test"}
    assert await repository.get_run(str(run.id)) == run

    updated = await repository.update_status(str(run.id), "running")
    assert updated.status == "running"
    assert updated.version == 1

    first = await repository.append_event(
        str(run.id), "run.started", {"safe": True}
    )
    second = await repository.append_event(
        str(run.id), "tool.called", {"token": "masked-by-test-contract"}
    )
    assert [first.sequence, second.sequence] == [1, 2]
    assert [event.event_type for event in await repository.list_events(str(run.id))] == [
        "run.started",
        "tool.called",
    ]

    checkpoint = await repository.save_checkpoint(
        str(run.id), {"step": 2, "state": ["read", "patch"]}
    )
    assert checkpoint.sequence == 1
    assert checkpoint.state == {"step": 2, "state": ["read", "patch"]}


@pytest.mark.asyncio
async def test_run_repository_rejects_stale_status_update(tmp_path):
    repository = RunRepository(tmp_path / "runs.db")
    await repository.init()
    run = await repository.create_run("code_task", {"task": "x"}, "workspace-1")

    current = await repository.update_status(str(run.id), "running", expected_version=0)
    assert current.version == 1

    with pytest.raises(RunRepositoryConflict):
        await repository.update_status(
            str(run.id), "completed", expected_version=0
        )


@pytest.mark.asyncio
async def test_run_repository_unknown_run_is_explicit(tmp_path):
    repository = RunRepository(tmp_path / "runs.db")
    await repository.init()

    assert await repository.get_run("missing") is None
    with pytest.raises(KeyError):
        await repository.update_status("missing", "running")
    with pytest.raises(KeyError):
        await repository.append_event("missing", "run.started", {})
    with pytest.raises(KeyError):
        await repository.save_checkpoint("missing", {})


@pytest.mark.asyncio
async def test_run_repository_redacts_sensitive_values_before_persisting(tmp_path):
    repository = RunRepository(tmp_path / "runs.db")
    await repository.init()
    run = await repository.create_run(
        "code_task",
        {"task": "x", "api_key": "request-secret"},
        "workspace-1",
    )

    await repository.append_event(
        str(run.id),
        "tool.called",
        {"token": "event-secret", "nested": {"password": "nested-secret"}},
    )
    checkpoint = await repository.save_checkpoint(
        str(run.id), {"state": {"authorization": "checkpoint-secret"}}
    )
    await repository.update_status(
        str(run.id),
        "failed",
        error={"credential": "error-secret"},
    )

    persisted_run = await repository.get_run(str(run.id))
    persisted_events = await repository.list_events(str(run.id))

    assert persisted_run is not None
    assert persisted_run.request["api_key"] == "[REDACTED]"
    assert persisted_run.error == {"credential": "[REDACTED]"}
    assert persisted_events[0].payload == {
        "token": "[REDACTED]",
        "nested": {"password": "[REDACTED]"},
    }
    assert checkpoint.state == {"state": {"authorization": "[REDACTED]"}}

    async with aiosqlite.connect(repository.path) as db:
        cursor = await db.execute(
            "SELECT state FROM task_run_checkpoints WHERE run_id = ?",
            (str(run.id),),
        )
        (checkpoint_state,) = await cursor.fetchone()
    assert json.loads(checkpoint_state) == {
        "state": {"authorization": "[REDACTED]"}
    }


def test_run_status_is_restricted():
    assert RunStatus.__args__ == (
        "queued",
        "running",
        "waiting",
        "completed",
        "failed",
        "cancelled",
        "timed_out",
    )
    assert RunRecord.model_fields["status"].annotation == RunStatus
