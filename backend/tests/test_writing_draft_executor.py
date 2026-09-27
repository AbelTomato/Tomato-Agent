import asyncio
import pytest

from app.writing.execution_models import DraftConfig, ExecutionConfig, WritingExecutionError
from app.writing.execution_repository import WritingExecutionRepository
from app.writing.executor import WritingTaskExecutor
from app.writing.models import WritingStatus
from app.writing.repository import WritingRepository
from app.writing.service import WritingConflictError, WritingService
from app.sessions.repository import SessionRepository


class FakeDraftGenerator:
    def __init__(self, *, event: asyncio.Event | None = None, error: Exception | None = None):
        self.calls = 0
        self.event = event
        self.error = error

    async def generate(self, topic, outline, citations, *, config):
        self.calls += 1
        if self.event is not None:
            await self.event.wait()
        if self.error is not None:
            raise self.error
        return "# Redis\n\n## 过期\n\nSETEX 设置过期时间。\n\n> 来源：chunk-1\n"


@pytest.fixture
async def components(tmp_path):
    path = tmp_path / "writing-draft.db"
    sessions = SessionRepository(path)
    await sessions.init()
    session_id = await sessions.create_session()
    writing = WritingRepository(path)
    await writing.init()
    service = WritingService(writing, draft_directory=tmp_path / "drafts")
    from app.knowledge.service import CitationSnapshot

    citation = CitationSnapshot(
        citation_id="chunk-1", chunk_id="chunk-1", document_id="doc-1",
        document_version="v1", source_path="redis.md", source_url=None,
        title="Redis", heading_path="过期", start_line=1, end_line=1,
        text="SETEX 设置过期时间。",
    )
    task = await service.create_task(session_id, "Redis")
    task = await service.publish_outline(
        task.task_id,
        {
            "title": "Redis",
            "sections": [{"title": "过期", "points": ["说明 SETEX"], "citation_ids": ["chunk-1"]}],
            "gaps": [],
        },
        [citation],
    )
    task = await service.confirm_outline(
        task.task_id, expected_version=task.version, outline=task.outline,
    )
    executions = WritingExecutionRepository(path)
    await executions.init()
    return tmp_path, service, executions, task


def make_executor(service, executions, generator):
    return WritingTaskExecutor(
        service, executions, object(), object(), config=ExecutionConfig(),
        model_id="fake-model", draft_generator=generator, draft_config=DraftConfig(),
    )


@pytest.mark.asyncio
async def test_success_publishes_draft_without_writing_file(components):
    tmp_path, service, executions, task = components
    generator = FakeDraftGenerator()
    executor = make_executor(service, executions, generator)

    result = await executor.execute_draft(task.task_id, expected_version=task.version)

    assert result.status == WritingStatus.AWAITING_SAVE_CONFIRMATION
    assert result.version == task.version + 1
    assert result.draft is not None
    assert result.drafting_run_id is not None
    attempt = await executions.get_attempt(task.task_id, task.version, kind="draft")
    assert attempt is not None and attempt.status == "completed"
    assert generator.calls == 1
    assert not (tmp_path / "drafts").exists()


@pytest.mark.asyncio
async def test_repeated_success_does_not_call_generator_again(components):
    _, service, executions, task = components
    generator = FakeDraftGenerator()
    executor = make_executor(service, executions, generator)

    first = await executor.execute_draft(task.task_id, expected_version=task.version)
    repeated = await executor.execute_draft(task.task_id, expected_version=task.version)

    assert repeated == first
    assert generator.calls == 1


@pytest.mark.asyncio
async def test_running_duplicate_and_stale_version_are_conflicts(components):
    _, service, executions, task = components
    event = asyncio.Event()
    generator = FakeDraftGenerator(event=event)
    executor = make_executor(service, executions, generator)
    pending = asyncio.create_task(executor.execute_draft(task.task_id, expected_version=task.version))
    while generator.calls == 0:
        await asyncio.sleep(0)

    with pytest.raises(WritingConflictError):
        await executor.execute_draft(task.task_id, expected_version=task.version)
    event.set()
    result = await pending
    assert result.version == task.version + 1
    with pytest.raises(WritingConflictError):
        await executor.execute_draft(task.task_id, expected_version=task.version - 1)


@pytest.mark.asyncio
async def test_provider_failure_marks_draft_attempt_and_task(components):
    _, service, executions, task = components
    executor = make_executor(service, executions, FakeDraftGenerator(error=RuntimeError("private")))

    with pytest.raises(WritingExecutionError) as error:
        await executor.execute_draft(task.task_id, expected_version=task.version)

    assert error.value.code == "provider_failed"
    current = await service.get_task(task.task_id)
    assert current.status == WritingStatus.FAILED
    assert current.failed_stage == WritingStatus.DRAFTING
    attempt = await executions.get_attempt(task.task_id, task.version, kind="draft")
    assert attempt is not None and attempt.error_code == "provider_failed"