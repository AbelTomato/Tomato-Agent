import asyncio
from uuid import uuid4

import aiosqlite
import pytest

from app.knowledge.service import CitationSnapshot
from app.sessions.repository import SessionRepository
from app.writing.execution_models import ExecutionConfig, GeneratedOutline, OutlineSection
from app.writing.execution_repository import WritingExecutionRepository
from app.writing.models import WritingStatus
from app.writing.repository import WritingRepository
from app.writing.service import WritingConflictError


@pytest.fixture
async def repositories(tmp_path):
    path = tmp_path / "writing-execution.db"
    sessions = SessionRepository(path)
    await sessions.init()
    session_id = await sessions.create_session()
    writing = WritingRepository(path)
    await writing.init()
    task = await writing.create_task(session_id, "Redis")
    executions = WritingExecutionRepository(path)
    await executions.init()
    return path, sessions, writing, executions, session_id, task


def citation():
    return CitationSnapshot(
        citation_id="chunk-1", chunk_id="chunk-1", document_id="doc-1",
        document_version="v1", source_path="redis.md", source_url=None,
        title="Redis", heading_path="过期", start_line=1, end_line=1,
        text="SETEX 设置过期时间。",
    )


def outline():
    return GeneratedOutline(
        title="Redis 过期机制",
        sections=[OutlineSection(title="设置过期", points=["说明 SETEX"], citation_ids=["chunk-1"])],
        gaps=[],
    )


@pytest.mark.asyncio
async def test_start_attempt_persists_linked_run_and_rejects_duplicate(repositories):
    _, sessions, _, executions, session_id, task = repositories

    attempt = await executions.start_attempt(
        task.task_id, expected_version=task.version,
        config=ExecutionConfig(), model_id="fake-model",
    )
    run = await sessions.get_run(attempt.run_id, session_id)
    assert run is not None
    assert run.status == "running"
    assert run.state["task_id"] == str(task.task_id)
    assert run.state["attempt_id"] == str(attempt.attempt_id)
    assert await executions.get_attempt(task.task_id, task.version) == attempt
    with pytest.raises(WritingConflictError):
        await executions.start_attempt(
            task.task_id, expected_version=task.version,
            config=ExecutionConfig(), model_id="fake-model",
        )


@pytest.mark.asyncio
async def test_start_attempt_rejects_wrong_version_and_missing_task(repositories):
    _, _, _, executions, _, task = repositories
    with pytest.raises(WritingConflictError):
        await executions.start_attempt(
            task.task_id, expected_version=task.version + 1,
            config=ExecutionConfig(), model_id="fake-model",
        )
    from app.writing.service import WritingNotFoundError

    with pytest.raises(WritingNotFoundError):
        await executions.start_attempt(
            uuid4(), expected_version=task.version,
            config=ExecutionConfig(), model_id="fake-model",
        )


@pytest.mark.asyncio
async def test_complete_attempt_commits_task_attempt_and_run_atomically(repositories):
    _, sessions, writing, executions, session_id, task = repositories
    attempt = await executions.start_attempt(
        task.task_id, expected_version=task.version,
        config=ExecutionConfig(), model_id="fake-model",
    )
    await executions.set_phase(attempt.attempt_id, "validation")
    result = await executions.complete_attempt(
        attempt.attempt_id, outline=outline(), citations=[citation()],
    )
    stored_attempt = await executions.get_attempt(task.task_id, task.version)
    run = await sessions.get_run(attempt.run_id, session_id)
    assert result.status == WritingStatus.AWAITING_OUTLINE_CONFIRMATION
    assert result.version == task.version + 1
    assert result.research_run_id == attempt.run_id
    assert result.outline == outline().model_dump(mode="json")
    assert result.citations == [citation()]
    assert stored_attempt.status == "completed"
    assert run.status == "completed"
    assert await writing.get_task(task.task_id) == result


@pytest.mark.asyncio
async def test_failed_attempt_updates_task_and_run_atomically(repositories):
    _, sessions, _, executions, session_id, task = repositories
    attempt = await executions.start_attempt(
        task.task_id, expected_version=task.version,
        config=ExecutionConfig(), model_id="fake-model",
    )
    failed = await executions.fail_attempt(attempt.attempt_id, error_code="provider_failed")
    stored = await executions.get_attempt(task.task_id, task.version)
    run = await sessions.get_run(attempt.run_id, session_id)
    assert failed.status == "failed"
    assert failed.version == task.version + 1
    assert failed.failed_stage == "researching"
    assert stored is not None and stored.status == "failed"
    assert stored.error_code == "provider_failed"
    assert run is not None and run.status == "failed"


@pytest.mark.asyncio
async def test_failed_run_update_rolls_back_complete_transaction(repositories):
    path, sessions, writing, executions, session_id, task = repositories
    attempt = await executions.start_attempt(
        task.task_id, expected_version=task.version,
        config=ExecutionConfig(), model_id="fake-model",
    )
    async with aiosqlite.connect(path) as db:
        await db.execute("""CREATE TRIGGER reject_run_completion
            BEFORE UPDATE ON runs WHEN NEW.status = 'completed'
            BEGIN SELECT RAISE(ABORT, 'injected'); END""")
        await db.commit()

    with pytest.raises(aiosqlite.IntegrityError):
        await executions.complete_attempt(attempt.attempt_id, outline=outline(), citations=[citation()])

    current = await writing.get_task(task.task_id)
    stored = await executions.get_attempt(task.task_id, task.version)
    run = await sessions.get_run(attempt.run_id, session_id)
    assert current.status == WritingStatus.RESEARCHING
    assert current.version == task.version
    assert current.outline == {}
    assert current.citations == []
    assert stored.status == "running"
    assert run.status == "running"


@pytest.mark.asyncio
async def test_stale_completion_marks_attempt_conflicted_without_mutating_new_task(repositories):
    _, sessions, writing, executions, session_id, task = repositories
    attempt = await executions.start_attempt(
        task.task_id, expected_version=task.version,
        config=ExecutionConfig(), model_id="fake-model",
    )
    async with aiosqlite.connect(writing.path) as db:
        await db.execute(
            "UPDATE writing_tasks SET version = version + 1 WHERE task_id = ?",
            (str(task.task_id),),
        )
        await db.commit()

    with pytest.raises(WritingConflictError):
        await executions.complete_attempt(attempt.attempt_id, outline=outline(), citations=[citation()])

    current = await writing.get_task(task.task_id)
    stored = await executions.get_attempt(task.task_id, task.version)
    run = await sessions.get_run(attempt.run_id, session_id)
    assert current.version == task.version + 1
    assert current.status == WritingStatus.RESEARCHING
    assert current.outline == {}
    assert stored.status == "conflicted"
    assert stored.error_code == "stale_result"
    assert run.status == "failed"


@pytest.mark.asyncio
async def test_concurrent_start_creates_only_one_attempt_and_run(repositories):
    _, sessions, _, executions, session_id, task = repositories

    async def start():
        try:
            return await executions.start_attempt(
                task.task_id, expected_version=task.version,
                config=ExecutionConfig(), model_id="fake-model",
            )
        except WritingConflictError:
            return None

    results = await asyncio.gather(start(), start())
    assert sum(result is not None for result in results) == 1
    async with aiosqlite.connect(sessions.path) as db:
        runs = await db.execute_fetchall("SELECT id FROM runs")
        attempts = await db.execute_fetchall("SELECT attempt_id, run_id FROM writing_attempts")
    assert len(runs) == len(attempts) == 1
    assert runs[0][0] == attempts[0][1]


@pytest.mark.asyncio
async def test_latest_attempt_is_none_before_execution(repositories):
    _, _, _, executions, _, task = repositories
    assert await executions.get_latest_attempt(task.task_id) is None