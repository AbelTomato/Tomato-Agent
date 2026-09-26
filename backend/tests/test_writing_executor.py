import asyncio
import json
from uuid import uuid4

import pytest

from app.agent.models import LLMResponse
from app.knowledge.service import CitationSnapshot
from app.sessions.repository import SessionRepository
from app.writing.execution_models import ExecutionConfig, ResearchBundle, WritingExecutionError
from app.writing.execution_repository import WritingExecutionRepository
from app.writing.executor import WritingTaskExecutor
from app.writing.outline import OutlineGenerator
from app.writing.repository import WritingRepository
from app.writing.service import WritingConflictError, WritingService


def citation():
    return CitationSnapshot(
        citation_id="chunk-1", chunk_id="chunk-1", document_id="doc-1",
        document_version="v1", source_path="redis.md", title="Redis",
        heading_path="过期", start_line=1, end_line=1, text="SETEX 设置过期时间。",
    )


class FakeResearcher:
    def __init__(self, *, status="supported", event=None):
        self.calls = 0
        self.event = event
        self.status = status

    async def collect(self, topic, *, config):
        self.calls += 1
        if self.event:
            await self.event.wait()
        return ResearchBundle(
            evidence_status=self.status,
            retrieval_mode="keyword",
            citations=[] if self.status != "supported" else [citation()],
        )


class FakeLLM:
    def __init__(self, *, content=None, error=None, event=None):
        self.calls = 0
        self.messages = None
        self.content = content or json.dumps({
            "title": "Redis", "sections": [{"title": "过期", "points": ["设置期限"],
            "citation_ids": ["chunk-1"]}], "gaps": [],
        }, ensure_ascii=False)
        self.error = error
        self.event = event

    async def complete(self, messages, tools):
        self.calls += 1
        self.messages = messages
        assert tools == []
        if self.event:
            await self.event.wait()
        if self.error:
            raise self.error
        return LLMResponse(kind="final", content=self.content)


@pytest.fixture
async def components(tmp_path):
    path = tmp_path / "execution.db"
    sessions = SessionRepository(path)
    await sessions.init()
    session_id = await sessions.create_session()
    writing_repo = WritingRepository(path)
    await writing_repo.init()
    service = WritingService(writing_repo, draft_directory=tmp_path / "drafts")
    task = await service.create_task(session_id, "Redis")
    executions = WritingExecutionRepository(path)
    await executions.init()
    return tmp_path, sessions, service, executions, task


def make_executor(service, executions, researcher=None, llm=None, timeout=1.0):
    researcher = researcher or FakeResearcher()
    llm = llm or FakeLLM()
    executor = WritingTaskExecutor(
        service, executions, researcher, OutlineGenerator(llm),
        config=ExecutionConfig(timeout_seconds=timeout), model_id="fake-model",
    )
    return executor, researcher, llm


@pytest.mark.asyncio
async def test_success_stops_at_outline_confirmation(components):
    tmp_path, sessions, service, executions, task = components
    executor, researcher, llm = make_executor(service, executions)

    result = await executor.execute_research(task.task_id, expected_version=task.version)

    assert result.status == "awaiting_outline_confirmation"
    assert result.version == task.version + 1
    assert result.draft is None and result.saved_path is None
    assert result.research_run_id is not None
    attempt = await executions.get_latest_attempt(task.task_id)
    assert attempt.status == "completed" and attempt.run_id == result.research_run_id
    run = await sessions.get_run(attempt.run_id, task.session_id)
    assert run.status == "completed"
    assert await sessions.list_events(attempt.run_id) == []
    assert researcher.calls == llm.calls == 1
    assert not (tmp_path / "drafts").exists()


@pytest.mark.asyncio
async def test_provider_failure_is_persisted_without_outline(components):
    tmp_path, _, service, executions, task = components
    executor, _, _ = make_executor(service, executions, llm=FakeLLM(error=RuntimeError("private")))

    with pytest.raises(WritingExecutionError) as error:
        await executor.execute_research(task.task_id, expected_version=task.version)

    assert error.value.code == "provider_failed"
    current = await service.get_task(task.task_id)
    attempt = await executions.get_attempt(task.task_id, task.version)
    assert current.status == "failed" and current.outline == {} and current.citations == []
    assert attempt.status == "failed" and attempt.error_code == "provider_failed"
    assert current.draft is None and not (tmp_path / "drafts").exists()


@pytest.mark.asyncio
async def test_completed_request_is_idempotent_even_after_confirmation(components):
    _, _, service, executions, task = components
    executor, researcher, llm = make_executor(service, executions)
    result = await executor.execute_research(task.task_id, expected_version=task.version)
    confirmed = await service.confirm_outline(
        task.task_id, expected_version=result.version, outline=result.outline,
    )

    repeated = await executor.execute_research(task.task_id, expected_version=task.version)

    assert repeated.status == confirmed.status == "drafting"
    assert repeated.version == confirmed.version
    assert researcher.calls == llm.calls == 1


@pytest.mark.asyncio
async def test_running_request_conflicts_without_second_external_call(components):
    _, _, service, executions, task = components
    gate = asyncio.Event()
    executor, researcher, llm = make_executor(service, executions, researcher=FakeResearcher(event=gate))
    first = asyncio.create_task(executor.execute_research(task.task_id, expected_version=task.version))
    for _ in range(100):
        attempt = await executions.get_attempt(task.task_id, task.version)
        if attempt is not None:
            break
        await asyncio.sleep(0)
    with pytest.raises(WritingConflictError):
        await executor.execute_research(task.task_id, expected_version=task.version)
    gate.set()
    await first
    assert researcher.calls == llm.calls == 1


@pytest.mark.asyncio
async def test_failed_attempt_requires_retry_version_before_new_run(components):
    _, _, service, executions, task = components
    failed_executor, _, _ = make_executor(service, executions, llm=FakeLLM(error=RuntimeError()))
    with pytest.raises(WritingExecutionError):
        await failed_executor.execute_research(task.task_id, expected_version=task.version)
    failed = await service.get_task(task.task_id)
    retry = await service.retry(task.task_id, expected_version=failed.version)
    next_executor, researcher, llm = make_executor(service, executions)

    result = await next_executor.execute_research(task.task_id, expected_version=retry.version)

    assert result.status == "awaiting_outline_confirmation"
    assert await executions.get_attempt(task.task_id, retry.version) is not None
    assert researcher.calls == llm.calls == 1


@pytest.mark.asyncio
async def test_evidence_error_is_recorded_and_old_version_conflicts(components):
    _, _, service, executions, task = components
    executor, researcher, llm = make_executor(service, executions, researcher=FakeResearcher(status="no_results"))
    with pytest.raises(WritingExecutionError) as error:
        await executor.execute_research(task.task_id, expected_version=task.version)
    assert error.value.code == "evidence_insufficient"
    assert llm.calls == 0
    assert (await executions.get_attempt(task.task_id, task.version)).error_code == "evidence_insufficient"
    with pytest.raises(WritingConflictError):
        await executor.execute_research(task.task_id, expected_version=task.version)


@pytest.mark.asyncio
async def test_running_attempt_is_not_taken_over_after_interrupted_process(components):
    _, _, service, executions, task = components
    attempt = await executions.start_attempt(
        task.task_id, expected_version=task.version,
        config=ExecutionConfig(), model_id="fake-model",
    )
    executor, researcher, llm = make_executor(service, executions)

    with pytest.raises(WritingConflictError):
        await executor.execute_research(task.task_id, expected_version=task.version)

    assert (await executions.get_attempt(task.task_id, task.version)).attempt_id == attempt.attempt_id
    assert researcher.calls == llm.calls == 0


@pytest.mark.asyncio
async def test_stale_expected_version_never_starts_external_work(components):
    _, _, service, executions, task = components
    executor, researcher, llm = make_executor(service, executions)

    with pytest.raises(WritingConflictError):
        await executor.execute_research(task.task_id, expected_version=task.version - 1)

    assert await executions.get_latest_attempt(task.task_id) is None
    assert researcher.calls == llm.calls == 0


@pytest.mark.asyncio
async def test_timeout_is_recorded(components):
    _, _, service, executions, task = components
    gate = asyncio.Event()
    executor, _, _ = make_executor(service, executions, researcher=FakeResearcher(event=gate), timeout=0.01)
    with pytest.raises(WritingExecutionError) as error:
        await executor.execute_research(task.task_id, expected_version=task.version)
    assert error.value.code == "deadline_exceeded"
    attempt = await executions.get_attempt(task.task_id, task.version)
    assert attempt.status == "failed" and attempt.error_code == "deadline_exceeded"


@pytest.mark.asyncio
async def test_cancelled_execution_is_recorded_and_propagated(components):
    _, _, service, executions, task = components
    gate = asyncio.Event()
    executor, _, _ = make_executor(service, executions, researcher=FakeResearcher(event=gate))
    pending = asyncio.create_task(executor.execute_research(task.task_id, expected_version=task.version))
    for _ in range(100):
        if await executions.get_attempt(task.task_id, task.version):
            break
        await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    attempt = await executions.get_attempt(task.task_id, task.version)
    assert attempt.status == "failed" and attempt.error_code == "cancelled"


@pytest.mark.asyncio
async def test_cancelled_loser_does_not_mark_winner_attempt_failed(components):
    _, _, service, executions, task = components
    original_start = executions.start_attempt
    winner_id = uuid4()
    loser_id = uuid4()

    async def cancelled_after_commit(*args, **kwargs):
        kwargs["attempt_id"] = winner_id
        await original_start(*args, **kwargs)
        kwargs["attempt_id"] = loser_id
        raise asyncio.CancelledError

    executions.start_attempt = cancelled_after_commit
    executor, researcher, llm = make_executor(service, executions)
    with pytest.raises(asyncio.CancelledError):
        await executor.execute_research(task.task_id, expected_version=task.version)

    stored = await executions.get_attempt(task.task_id, task.version)
    assert stored is not None
    assert stored.attempt_id == winner_id
    assert stored.status == "running"
    assert researcher.calls == llm.calls == 0


@pytest.mark.asyncio
async def test_publication_requires_matching_completed_run(components, monkeypatch):
    _, sessions, service, executions, task = components
    executor, _, _ = make_executor(service, executions)
    attempt = await executions.start_attempt(
        task.task_id, expected_version=task.version,
        config=ExecutionConfig(), model_id="fake-model",
    )

    await sessions.update_run(
        attempt.run_id,
        "completed",
        {"task_id": str(task.task_id), "attempt_id": str(uuid4()), "phase": "publication"},
    )
    resolved = await executor._read_publication(attempt.attempt_id)
    assert resolved is None

    await sessions.update_run(
        attempt.run_id,
        "completed",
        {"task_id": str(task.task_id), "attempt_id": str(attempt.attempt_id), "phase": "publication"},
    )
    resolved = await executor._read_publication(attempt.attempt_id)
    assert resolved is not None
    assert resolved[2]["status"] == "completed"


@pytest.mark.asyncio
async def test_attempt_lookup_storage_error_uses_stable_error_code(components, monkeypatch):
    _, _, service, executions, task = components
    executor, researcher, llm = make_executor(service, executions)

    async def unreadable_attempt(*args, **kwargs):
        raise RuntimeError("database is unavailable")

    monkeypatch.setattr(executions, "get_attempt", unreadable_attempt)
    with pytest.raises(WritingExecutionError) as error:
        await executor.execute_research(task.task_id, expected_version=task.version)
    assert error.value.code == "storage_unavailable"
    assert researcher.calls == llm.calls == 0