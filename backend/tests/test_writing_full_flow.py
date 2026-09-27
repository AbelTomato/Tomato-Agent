from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.agent.models import LLMResponse
from app.api.writing import router
from app.knowledge.models import Chunk, Document
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.service import KnowledgeService
from app.sessions.repository import SessionRepository
from app.writing.draft import DraftGenerator
from app.writing.execution_models import DraftConfig, ExecutionConfig, WritingExecutionError
from app.writing.execution_repository import WritingExecutionRepository
from app.writing.executor import WritingTaskExecutor
from app.writing.outline import OutlineGenerator
from app.writing.repository import WritingRepository
from app.writing.research import WritingResearcher
from app.writing.service import WritingService
from fastapi import FastAPI


class FakeWritingLLM:
    def __init__(self, *, fail_draft: bool = False) -> None:
        self.outline_calls = 0
        self.draft_calls = 0
        self.fail_draft = fail_draft
        self.draft_messages = []

    async def complete(self, messages, tools):
        if self.outline_calls == 0:
            self.outline_calls += 1
            return LLMResponse(
                kind="final",
                content=(
                    '{"title":"SETEX 过期机制","sections":[{"title":"过期设置",'
                    '"points":["SETEX 设置键值和过期秒数"],'
                    '"citation_ids":["chunk-setex"]}],"gaps":[]}'
                ),
            )
        self.draft_calls += 1
        self.draft_messages.append(messages)
        if self.fail_draft:
            raise RuntimeError("provider unavailable")
        return LLMResponse(
            kind="final",
            content=(
                '{"title":"SETEX 过期机制","sections":[{"title":"过期设置",'
                '"content":"SETEX 为键设置值及其过期时间。",'
                '"citation_ids":["chunk-setex"]}]}'
            ),
        )


@pytest.fixture
def rag_database_path():
    root = Path("/home/abeltomato/workspace/projects/Tomato-Agent/backend/data/rag/databases")
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"writing-full-flow-fixture-2026-09-27-{uuid4()}.db"
    yield path
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            candidate.unlink()


async def make_flow(tmp_path: Path, rag_database_path: Path, *, empty=False, fail_draft=False):
    knowledge = KnowledgeRepository(rag_database_path)
    await knowledge.init()
    if not empty:
        await knowledge.replace_document(
            Document("doc-redis", "redis.md", None, "Redis", "v1"),
            [Chunk("chunk-setex", "doc-redis", "v1", "Redis > SETEX", 1, 1,
                   "SETEX key value seconds sets a value and its expiration time.", 10)],
        )

    database = tmp_path / "business.db"
    sessions = SessionRepository(database)
    await sessions.init()
    repository = WritingRepository(database)
    await repository.init()
    attempts = WritingExecutionRepository(database)
    await attempts.init()
    service = WritingService(repository, draft_directory=tmp_path / "drafts")
    llm = FakeWritingLLM(fail_draft=fail_draft)
    executor = WritingTaskExecutor(
        service,
        attempts,
        WritingResearcher(KnowledgeService(knowledge, pipeline=None)),
        OutlineGenerator(llm),
        config=ExecutionConfig(),
        model_id="fake-writing-model",
        draft_generator=DraftGenerator(llm),
        draft_config=DraftConfig(),
    )
    session_id = await sessions.create_session()
    task = await service.create_task(session_id, "SETEX expiration")
    app = FastAPI()
    app.state.writing_service = service
    app.state.writing_executor = executor
    app.include_router(router)
    return app, service, executor, llm, task


@pytest.mark.asyncio
async def test_full_flow_edited_outline_repeated_draft_and_idempotent_save(
    tmp_path, rag_database_path
):
    app, service, executor, llm, task = await make_flow(tmp_path, rag_database_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        researching = await client.post(
            f"/api/writing-tasks/{task.task_id}/research", json={"version": task.version}
        )
        assert researching.status_code == 200
        outline_task = researching.json()
        assert outline_task["status"] == "awaiting_outline_confirmation"

        missing = await client.post(
            f"/api/writing-tasks/{uuid4()}/draft", json={"version": outline_task["version"]}
        )
        assert missing.status_code == 404

        not_confirmed = await client.post(
            f"/api/writing-tasks/{task.task_id}/draft", json={"version": outline_task["version"]}
        )
        assert not_confirmed.status_code == 409
        assert llm.draft_calls == 0

        edited_outline = outline_task["outline"] | {"title": "用户修订：SETEX"}
        confirmed = await client.post(
            f"/api/writing-tasks/{task.task_id}/confirm-outline",
            json={"version": outline_task["version"], "outline": edited_outline},
        )
        assert confirmed.status_code == 200
        confirmed_task = confirmed.json()
        assert confirmed_task["status"] == "drafting"
        assert confirmed_task["outline"]["title"] == "用户修订：SETEX"

        generated = await client.post(
            f"/api/writing-tasks/{task.task_id}/draft", json={"version": confirmed_task["version"]}
        )
        assert generated.status_code == 200
        draft_task = generated.json()
        assert draft_task["status"] == "awaiting_save_confirmation"
        assert "用户修订：SETEX" in str(llm.draft_messages[0])

        stale_draft = await client.post(
            f"/api/writing-tasks/{task.task_id}/draft",
            json={"version": confirmed_task["version"] - 1},
        )
        assert stale_draft.status_code == 409
        unchanged = await service.get_task(task.task_id)
        assert unchanged.version == draft_task["version"]
        assert unchanged.status == "awaiting_save_confirmation"

        duplicate = await executor.execute_draft(task.task_id, expected_version=confirmed_task["version"])
        assert duplicate.status == "awaiting_save_confirmation"
        assert llm.draft_calls == 1

        saved = await client.post(
            f"/api/writing-tasks/{task.task_id}/save",
            json={"version": draft_task["version"], "idempotency_key": "full-flow-save-1"},
        )
        assert saved.status_code == 200
        saved_task = saved.json()
        saved_path = Path(saved_task["saved_path"])
        assert saved_task["status"] == "saved"
        assert saved_path.read_text(encoding="utf-8") == draft_task["draft"]
        assert list((tmp_path / "drafts").glob("*.md")) == [saved_path]

        repeated_save = await client.post(
            f"/api/writing-tasks/{task.task_id}/save",
            json={"version": draft_task["version"], "idempotency_key": "full-flow-save-1"},
        )
        assert repeated_save.status_code == 200
        assert repeated_save.json() == saved_task
        assert llm.outline_calls == 1
        assert llm.draft_calls == 1


@pytest.mark.asyncio
async def test_insufficient_evidence_does_not_call_model_or_create_file(
    tmp_path, rag_database_path
):
    _, service, executor, llm, task = await make_flow(
        tmp_path, rag_database_path, empty=True
    )
    with pytest.raises(WritingExecutionError) as error:
        await executor.execute_research(task.task_id, expected_version=task.version)
    assert error.value.code == "evidence_insufficient"
    current = await service.get_task(task.task_id)
    assert current.status == "failed"
    assert llm.outline_calls == llm.draft_calls == 0
    assert not list((tmp_path / "drafts").glob("*.md"))


@pytest.mark.asyncio
async def test_draft_provider_failure_is_stable_and_does_not_save(
    tmp_path, rag_database_path
):
    _, service, executor, llm, task = await make_flow(
        tmp_path, rag_database_path, fail_draft=True
    )
    researched = await executor.execute_research(task.task_id, expected_version=task.version)
    confirmed = await service.confirm_outline(
        task.task_id,
        expected_version=researched.version,
        outline=researched.outline,
    )
    with pytest.raises(WritingExecutionError) as error:
        await executor.execute_draft(task.task_id, expected_version=confirmed.version)
    assert error.value.code == "provider_failed"
    current = await service.get_task(task.task_id)
    assert current.status == "failed"
    assert current.failed_stage == "drafting"
    assert llm.outline_calls == llm.draft_calls == 1
    assert not list((tmp_path / "drafts").glob("*.md"))