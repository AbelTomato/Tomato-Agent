import json

import aiosqlite
import pytest

from app.agent.models import LLMResponse
from app.memory.extraction import MemoryExtractor
from app.memory.models import MemoryTurnIngest
from app.memory.repository import MemoryRepository
from app.memory.worker import MemoryWorker


class QueueLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []

    async def complete(self, messages, tools):
        self.messages.append(messages)
        return LLMResponse(kind="final", content=json.dumps(self.responses.pop(0), ensure_ascii=False))


def extracted(*, content="喜欢中文回答", message_id="u1", topic="response.language",
              confidence=0.99, durable=True):
    return {"facts": [{
        "kind": "preference", "topic_key": topic, "content": content,
        "evidence_message_ids": [message_id], "confidence": confidence,
        "self_reported": True, "durable": durable,
    }]}


def turn(text, *, session="session-1"):
    return {
        "source_system": "tomato-desktop",
        "source_installation_id": "install-1",
        "session_id": session,
        "messages": [
            {"message_id": "a1", "role": "assistant", "content": "你似乎喜歡英文。", "revision": 1},
            {"message_id": "u1", "role": "user", "content": text, "revision": 1},
        ],
    }


@pytest.mark.asyncio
async def test_extractor_prompt_excludes_assistant_as_independent_evidence():
    llm = QueueLLM([{"facts": []}])
    extractor = MemoryExtractor(llm)
    await extractor.extract(turn("我喜欢中文回答")["messages"])
    prompt = llm.messages[0][0].content
    assert "我喜欢中文回答" in prompt
    assert "你似乎喜歡英文" not in prompt


@pytest.mark.asyncio
async def test_worker_persists_only_clear_durable_user_fact_with_source(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    llm = QueueLLM([extracted()])
    worker = MemoryWorker(repository, MemoryExtractor(llm))
    queued = await repository.enqueue_turn(scope_id="trusted-user", value=turn("我喜欢中文回答"))

    assert await worker.run_once()
    items = await repository.list_items(scope="personal", scope_id="trusted-user")
    assert len(items) == 1
    assert items[0].confirmation == "automatic"
    assert (await repository.sources(items[0].item_id, 1))[0]["role"] == "user"
    assert (await repository.claim_job()) is None
    async with aiosqlite.connect(repository.path) as db:
        row = await (await db.execute(
            "SELECT status,payload FROM memory_jobs WHERE job_id=?", (queued["job_id"],),
        )).fetchone()
    assert row == ("completed", None)


@pytest.mark.asyncio
async def test_temporary_fact_and_conflict_are_candidates_and_acceptance_supersedes(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    llm = QueueLLM([
        extracted(content="喜欢中文回答"),
        extracted(content="喜欢英文回答"),
        extracted(content="这次请用法语", topic="response.language", durable=False),
        extracted(content="这次请用法语", topic="task.language", durable=False),
    ])
    worker = MemoryWorker(repository, MemoryExtractor(llm))
    await repository.enqueue_turn(scope_id="trusted-user", value=turn("我喜欢中文回答"))
    await worker.run_once()
    original = (await repository.list_items(scope="personal", scope_id="trusted-user"))[0]

    await repository.enqueue_turn(scope_id="trusted-user", value=turn("我更喜欢英文回答", session="session-2"))
    await worker.run_once()
    candidates = await repository.list_candidates(scope_id="trusted-user")
    assert len(candidates) == 1
    assert candidates[0]["reason"] == "conflict"
    accepted = await repository.decide_candidate(
        candidates[0]["candidate_id"], scope_id="trusted-user", action="accept",
    )
    assert accepted["status"] == "accepted"
    assert (await repository.get(original.item_id)).status == "superseded"
    assert len(await repository.list_items(scope="personal", scope_id="trusted-user")) == 1

    await repository.enqueue_turn(scope_id="trusted-user", value=turn("这次请用法语", session="session-3"))
    await worker.run_once()
    candidates = await repository.list_candidates(scope_id="trusted-user")
    assert candidates[0]["reason"] == "conflict"
    assert len(await repository.list_items(scope="personal", scope_id="trusted-user")) == 1

    await repository.enqueue_turn(scope_id="trusted-user", value=turn("这次请用法语", session="session-4"))
    await worker.run_once()
    temporary = [item for item in await repository.list_candidates(scope_id="trusted-user")
                 if item["topic_key"] == "task.language"]
    assert len(temporary) == 1
    assert temporary[0]["reason"] == "needs_review"
    assert len(await repository.list_items(scope="personal", scope_id="trusted-user")) == 1


@pytest.mark.asyncio
async def test_unknown_or_assistant_evidence_ids_never_create_memory(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    llm = QueueLLM([extracted(message_id="a1")])
    worker = MemoryWorker(repository, MemoryExtractor(llm))
    await repository.enqueue_turn(scope_id="trusted-user", value=turn("我喜欢中文回答"))
    await worker.run_once()
    assert await repository.list_items(scope="personal", scope_id="trusted-user") == []
    assert await repository.list_candidates(scope_id="trusted-user") == []


@pytest.mark.asyncio
async def test_worker_restart_recovers_running_job(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    await repository.enqueue_turn(scope_id="trusted-user", value=turn("我喜欢中文回答"))
    job = await repository.claim_job()
    assert job is not None
    restarted = MemoryRepository(repository.path)
    await restarted.init()
    recovered = await restarted.claim_job()
    assert recovered is not None
    assert recovered["job_id"] == job["job_id"]
    assert recovered["attempts"] == 2


@pytest.mark.asyncio
async def test_source_tombstone_blocks_queued_extraction(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    payload = turn("我喜欢中文回答")
    await repository.enqueue_turn(scope_id="trusted-user", value=payload)
    await repository.delete_source({
        "source_system": "tomato-desktop",
        "source_installation_id": "install-1",
        "session_id": "session-1",
        "message_id": "u1",
        "source_revision": 1,
    }, request_id="delete-before-extract")
    worker = MemoryWorker(repository, MemoryExtractor(QueueLLM([extracted()])))
    assert await worker.run_once()
    assert await repository.list_items(scope="personal", scope_id="trusted-user") == []


@pytest.mark.asyncio
async def test_same_source_delivery_is_idempotent(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    first = await repository.enqueue_turn(scope_id="trusted-user", value=turn("我喜欢中文回答"))
    repeated = await repository.enqueue_turn(scope_id="trusted-user", value=turn("我喜欢中文回答"))
    assert repeated["duplicate"] is True
    assert repeated["job_id"] == first["job_id"]


@pytest.mark.asyncio
async def test_extraction_input_limit_rejects_instead_of_silently_truncating():
    payload = turn("x" * 3001)
    payload["messages"].append({
        "message_id": "u2", "role": "user", "content": "y" * 3000, "revision": 1,
    })
    with pytest.raises(ValueError, match="memory_extraction_input_too_large"):
        MemoryTurnIngest.model_validate(payload)


@pytest.mark.asyncio
async def test_failed_extraction_retries_with_same_idempotent_job(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    queued = await repository.enqueue_turn(scope_id="trusted-user", value=turn("我喜欢中文回答"))

    class FailingLLM:
        async def complete(self, messages, tools):
            raise RuntimeError("provider unavailable")

    worker = MemoryWorker(repository, MemoryExtractor(FailingLLM()))
    assert await worker.run_once()
    async with aiosqlite.connect(repository.path) as db:
        await db.execute("UPDATE memory_jobs SET retry_at='2000-01-01T00:00:00+00:00' WHERE job_id=?",
                         (queued["job_id"],))
        await db.commit()
        row = await (await db.execute(
            "SELECT status,attempts FROM memory_jobs WHERE job_id=?", (queued["job_id"],),
        )).fetchone()
    assert row == ("queued", 1)
    worker.extractor.llm = QueueLLM([{"facts": []}])
    assert await worker.run_once()
    async with aiosqlite.connect(repository.path) as db:
        row = await (await db.execute(
            "SELECT status,attempts,payload FROM memory_jobs WHERE job_id=?", (queued["job_id"],),
        )).fetchone()
    assert row == ("completed", 2, None)


@pytest.mark.asyncio
async def test_ingestion_api_uses_trusted_personal_scope_and_requires_worker(tmp_path):
    import httpx

    from app.application import create_app
    from app.settings import Settings

    app = create_app(Settings(
        _env_file=None, database_path=tmp_path / "agent.db", docs_root=tmp_path,
        memory_enabled=True, memory_auto_mode="automatic",
        desktop_environment_id="trusted-user",
    ))
    app.state.memory_worker = object()
    app.state.memory_extractor = object()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.post("/api/memory/sources/ingest", json={
                **turn("我喜欢中文回答"),
            })
            assert response.status_code == 200, response.text
            listed = await client.get("/api/memory/items", params={
                "scope": "personal", "scope_id": "trusted-user",
            })
            assert listed.json()["items"] == []
            pending = await client.get("/api/memory/status")
            assert pending.json()["enabled"] is True
            assert pending.json()["ready"] is True

    app = create_app(Settings(
        _env_file=None, database_path=tmp_path / "no-worker.db", docs_root=tmp_path,
        memory_enabled=True, memory_auto_mode="automatic", llm_api_key="",
    ))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            status = await client.get("/api/memory/status")
            assert status.json()["state"] == "degraded"
            response = await client.post("/api/memory/sources/ingest", json=turn("我喜欢中文回答"))
            assert response.status_code == 503


@pytest.mark.asyncio
async def test_manual_memory_is_ready_when_automatic_extraction_is_off(tmp_path):
    import httpx

    from app.application import create_app
    from app.settings import Settings

    app = create_app(Settings(
        _env_file=None, database_path=tmp_path / "manual.db", docs_root=tmp_path,
        memory_enabled=True, memory_auto_mode="off", llm_api_key="",
        desktop_environment_id="trusted-user",
    ))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            status = await client.get("/api/memory/status")
            assert status.json()["ready"] is True
            assert status.json()["state"] == "ready"
            ingest = await client.post("/api/memory/sources/ingest", json=turn("我喜欢中文回答"))
            assert ingest.status_code == 409


@pytest.mark.asyncio
async def test_explicit_intent_saves_and_supersedes_same_topic_with_user_evidence(tmp_path):
    import httpx

    from app.application import create_app
    from app.memory.models import MemoryItemCreate
    from app.settings import Settings

    app = create_app(Settings(
        _env_file=None, database_path=tmp_path / "agent.db", docs_root=tmp_path,
        memory_enabled=True, desktop_environment_id="trusted-user",
    ))
    app.state.memory_extractor = MemoryExtractor(QueueLLM([extracted(content="喜欢中文回答")]))
    async with app.router.lifespan_context(app):
        old = await app.state.memory_repository.create(MemoryItemCreate(
            kind="preference", scope="personal", scope_id="trusted-user",
            topic_key="response.language", content="喜欢英文回答", confirmation="explicit",
        ), request_id="old-explicit")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.post("/api/memory/intent", json={
                "source_system": "tomato-desktop",
                "source_installation_id": "install-1",
                "session_id": "session-explicit",
                "message": {
                    "message_id": "u1", "role": "user",
                    "content": "记住我喜欢中文回答", "revision": 1,
                },
            })
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "saved"
            current = await app.state.memory_repository.list_items(
                scope="personal", scope_id="trusted-user",
            )
            assert len(current) == 1
            assert current[0].content == "喜欢中文回答"
            assert (await app.state.memory_repository.get(old.item_id)).status == "superseded"
            assert (await app.state.memory_repository.sources(current[0].item_id, 1))[0]["message_id"] == "u1"
