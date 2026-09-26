from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app import main
from app.agent.models import LLMResponse
from app.knowledge.models import Chunk, Document
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.service import KnowledgeService
from app.sessions.repository import SessionRepository
from app.writing.execution_models import ExecutionConfig, WritingExecutionError
from app.writing.execution_repository import WritingExecutionRepository
from app.writing.executor import WritingTaskExecutor
from app.writing.outline import OutlineGenerator
from app.writing.repository import WritingRepository
from app.writing.research import WritingResearcher
from app.writing.service import WritingConflictError, WritingService


class FakeOutlineLLM:
    def __init__(self, citation_id: str = "chunk-setex") -> None:
        self.calls = 0
        self.messages = []
        self.citation_id = citation_id

    async def complete(self, messages, tools):
        self.calls += 1
        self.messages.append((messages, tools))
        return LLMResponse(
            kind="final",
            content=(
                '{"title":"SETEX 过期机制",'
                '"sections":[{"title":"设置过期时间",'
                '"points":["SETEX 为键设置值和过期时间"],'
                f'"citation_ids":["{self.citation_id}"]}}],"gaps":[]}}'
            ),
        )


@pytest.fixture
def rag_database_path():
    root = Path(
        "/home/abeltomato/workspace/projects/Tomato-Agent/backend/data/rag/databases"
    )
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"writing-research-fixture-2026-09-26-{uuid4()}.db"
    assert not path.exists()
    yield path
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            candidate.unlink()


async def _make_components(tmp_path: Path, rag_database_path: Path, llm=None):
    knowledge_repository = KnowledgeRepository(rag_database_path)
    await knowledge_repository.init()
    await knowledge_repository.replace_document(
        Document("doc-redis", "redis.md", None, "Redis", "v1"),
        [
            Chunk(
                "chunk-setex",
                "doc-redis",
                "v1",
                "Redis > SETEX",
                1,
                1,
                "SETEX key value seconds sets a value and its expiration time.",
                10,
            )
        ],
    )
    knowledge_service = KnowledgeService(knowledge_repository, pipeline=None)

    business_path = tmp_path / "writing.db"
    sessions = SessionRepository(business_path)
    await sessions.init()
    writing_repository = WritingRepository(business_path)
    await writing_repository.init()
    execution_repository = WritingExecutionRepository(business_path)
    await execution_repository.init()
    writing_service = WritingService(
        writing_repository, draft_directory=tmp_path / "drafts"
    )
    llm = llm or FakeOutlineLLM()
    executor = WritingTaskExecutor(
        writing_service,
        execution_repository,
        WritingResearcher(knowledge_service),
        OutlineGenerator(llm),
        config=ExecutionConfig(),
        model_id="fake-outline-model",
    )
    return sessions, writing_service, execution_repository, executor, llm


@pytest.mark.asyncio
async def test_real_keyword_research_publishes_snapshot_and_stops_for_confirmation(
    monkeypatch, tmp_path, rag_database_path
):
    sessions, writing_service, execution_repository, executor, llm = await _make_components(
        tmp_path, rag_database_path
    )
    monkeypatch.setattr(main.app.state, "writing_service", writing_service)
    monkeypatch.setattr(
        main.app.state, "writing_execution_repository", execution_repository
    )
    monkeypatch.setattr(main.app.state, "writing_executor", executor)

    session_id = await sessions.create_session()
    task = await writing_service.create_task(session_id, "SETEX expiration")
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/writing-tasks/{task.task_id}/research",
            json={"version": task.version},
        )
        assert response.status_code == 200
        published = response.json()
        assert published["status"] == "awaiting_outline_confirmation"
        assert published["version"] == task.version + 1
        assert published["outline"]["sections"][0]["citation_ids"] == ["chunk-setex"]
        assert published["citations"][0]["text"].startswith("SETEX key")
        assert published["citations"][0]["document_version"] == "v1"
        assert published["draft"] is None
        assert published["saved_path"] is None

        attempt = await client.get(
            f"/api/writing-tasks/{task.task_id}/research-attempt"
        )
        assert attempt.status_code == 200
        assert attempt.json()["status"] == "completed"

        repeated = await client.post(
            f"/api/writing-tasks/{task.task_id}/research",
            json={"version": task.version},
        )
        assert repeated.status_code == 200
        assert repeated.json()["version"] == published["version"]
        assert llm.calls == 1

        confirmed = await client.post(
            f"/api/writing-tasks/{task.task_id}/confirm-outline",
            json={"version": published["version"], "outline": published["outline"]},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "drafting"
        assert not (tmp_path / "drafts").exists()

    stored_attempt = await execution_repository.get_latest_attempt(task.task_id)
    assert stored_attempt is not None
    run = await sessions.get_run(stored_attempt.run_id, session_id)
    assert run is not None
    assert run.state["phase"] == "publication"
    assert await sessions.list_session_events(session_id) == []


@pytest.mark.asyncio
async def test_research_snapshot_is_stable_and_sessions_do_not_share_evidence(
    tmp_path, rag_database_path
):
    sessions, writing_service, _, executor, llm = await _make_components(
        tmp_path, rag_database_path
    )
    first_session = await sessions.create_session()
    second_session = await sessions.create_session()
    first = await writing_service.create_task(first_session, "SETEX expiration")
    second = await writing_service.create_task(second_session, "SETEX expiration")

    first_result = await executor.execute_research(first.task_id, expected_version=first.version)
    second_result = await executor.execute_research(second.task_id, expected_version=second.version)
    assert first_result.citations[0].text == second_result.citations[0].text
    assert first_result.citations[0].document_version == "v1"
    assert first_result.research_run_id != second_result.research_run_id
    assert llm.calls == 2

    repository = KnowledgeRepository(rag_database_path)
    await repository.replace_document(
        Document("doc-redis", "redis.md", None, "Redis", "v2"),
        [
            Chunk(
                "chunk-setex",
                "doc-redis",
                "v2",
                "Redis > SETEX",
                1,
                1,
                "SETEX changed text.",
                5,
            )
        ],
    )
    current = await writing_service.get_task(first.task_id)
    assert current.citations[0].text.startswith("SETEX key")
    assert current.citations[0].document_version == "v1"


@pytest.mark.asyncio
async def test_empty_real_knowledge_database_fails_without_model_call(tmp_path, rag_database_path):
    knowledge_repository = KnowledgeRepository(rag_database_path)
    await knowledge_repository.init()
    knowledge_service = KnowledgeService(knowledge_repository, pipeline=None)
    business_path = tmp_path / "writing.db"
    sessions = SessionRepository(business_path)
    await sessions.init()
    writing_repository = WritingRepository(business_path)
    await writing_repository.init()
    execution_repository = WritingExecutionRepository(business_path)
    await execution_repository.init()
    writing_service = WritingService(writing_repository, draft_directory=tmp_path / "drafts")
    llm = FakeOutlineLLM()
    executor = WritingTaskExecutor(
        writing_service,
        execution_repository,
        WritingResearcher(knowledge_service),
        OutlineGenerator(llm),
        config=ExecutionConfig(),
        model_id="fake-outline-model",
    )
    task = await writing_service.create_task(await sessions.create_session(), "unknown")
    with pytest.raises(WritingExecutionError) as error:
        await executor.execute_research(task.task_id, expected_version=task.version)
    assert getattr(error.value, "code", None) == "evidence_insufficient"
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_unknown_model_citation_is_rejected_by_real_execution_chain(
    tmp_path, rag_database_path
):
    sessions, writing_service, _, executor, llm = await _make_components(
        tmp_path, rag_database_path, llm=FakeOutlineLLM("unknown-chunk")
    )
    task = await writing_service.create_task(
        await sessions.create_session(), "SETEX expiration"
    )
    with pytest.raises(WritingExecutionError) as error:
        await executor.execute_research(task.task_id, expected_version=task.version)
    assert error.value.code == "invalid_citation"
    failed = await writing_service.get_task(task.task_id)
    assert failed.status == "failed"
    assert failed.outline == {}
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_distinct_tasks_publish_only_their_own_evidence_snapshot(
    tmp_path, rag_database_path
):
    sessions, writing_service, _, first_executor, first_llm = await _make_components(
        tmp_path, rag_database_path
    )
    first = await writing_service.create_task(
        await sessions.create_session(), "SETEX expiration"
    )
    first_result = await first_executor.execute_research(
        first.task_id, expected_version=first.version
    )

    knowledge = KnowledgeRepository(rag_database_path)
    await knowledge.replace_document(
        Document("doc-other", "other.md", None, "Other", "v2"),
        [
            Chunk(
                "chunk-other", "doc-other", "v2", "Other > Expiration", 1, 1,
                "Other system expiration policy.", 5,
            )
        ],
    )
    second_llm = FakeOutlineLLM("chunk-other")
    second_executor = WritingTaskExecutor(
        writing_service,
        first_executor.repository,
        WritingResearcher(KnowledgeService(knowledge, pipeline=None)),
        OutlineGenerator(second_llm),
        config=ExecutionConfig(),
        model_id="fake-outline-model",
    )
    second = await writing_service.create_task(
        await sessions.create_session(), "Other expiration"
    )
    second_result = await second_executor.execute_research(
        second.task_id, expected_version=second.version
    )

    assert first_result.citations[0].citation_id == "chunk-setex"
    assert second_result.citations[0].citation_id == "chunk-other"
    assert first_result.citations[0].text != second_result.citations[0].text
    assert first_llm.calls == second_llm.calls == 1


@pytest.mark.asyncio
async def test_failed_research_requires_retry_before_research_can_run_again(
    tmp_path, rag_database_path
):
    empty_knowledge = KnowledgeRepository(rag_database_path)
    await empty_knowledge.init()
    sessions = SessionRepository(tmp_path / "writing.db")
    await sessions.init()
    writing_repository = WritingRepository(tmp_path / "writing.db")
    await writing_repository.init()
    execution_repository = WritingExecutionRepository(tmp_path / "writing.db")
    await execution_repository.init()
    writing_service = WritingService(
        writing_repository, draft_directory=tmp_path / "drafts"
    )
    llm = FakeOutlineLLM()
    executor = WritingTaskExecutor(
        writing_service,
        execution_repository,
        WritingResearcher(KnowledgeService(empty_knowledge, pipeline=None)),
        OutlineGenerator(llm),
        config=ExecutionConfig(),
        model_id="fake-outline-model",
    )
    task = await writing_service.create_task(
        await sessions.create_session(), "unknown keyword"
    )
    with pytest.raises(WritingExecutionError) as error:
        await executor.execute_research(task.task_id, expected_version=task.version)
    assert getattr(error.value, "code", None) == "evidence_insufficient"
    failed = await writing_service.get_task(task.task_id)
    assert failed.status == "failed"

    with pytest.raises(WritingConflictError):
        await executor.execute_research(task.task_id, expected_version=failed.version)

    retried = await writing_service.retry(task.task_id, expected_version=failed.version)
    assert retried.status == "researching"
    assert retried.version == failed.version + 1
    with pytest.raises(WritingExecutionError) as retried_error:
        await executor.execute_research(
            task.task_id, expected_version=retried.version
        )
    assert retried_error.value.code == "evidence_insufficient"