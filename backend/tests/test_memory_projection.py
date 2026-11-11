import json

import aiosqlite
import pytest

from app.agent.models import LLMResponse
from app.memory.models import MemoryItemCreate, MemorySourceInput
from app.memory.projection import MemoryProjectionBuilder
from app.memory.repository import MemoryRepository
from app.memory.worker import MemoryWorker


class SummaryLLM:
    def __init__(self, summary="偏好用简洁的中文回答。", *, before_return=None):
        self.summary = summary
        self.before_return = before_return
        self.calls = 0
        self.prompts = []

    async def complete(self, messages, tools):
        self.calls += 1
        self.prompts.append(messages[0].content)
        if self.before_return is not None:
            await self.before_return()
        return LLMResponse(kind="final", content=json.dumps({"summary": self.summary}, ensure_ascii=False))


def preference(*, topic="response.style", content="回答时偏好简洁说明", message_id="m1"):
    return MemoryItemCreate(
        kind="preference", scope="personal", scope_id="user-1", topic_key=topic,
        content=content, sources=[MemorySourceInput(
            source_system="desktop", source_installation_id="install-1",
            session_id="session-1", message_id=message_id, excerpt=content,
        )],
    )


@pytest.mark.asyncio
async def test_projection_worker_builds_source_bound_profile_even_with_extraction_off(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    item = await repository.create(preference(), request_id="summary-fact")
    llm = SummaryLLM()
    worker = MemoryWorker(
        repository, None, mode="off",
        projection_builder=MemoryProjectionBuilder(repository, llm),
    )

    assert await worker.run_once()
    projection = await repository.get_projection(
        scope="personal", scope_id="user-1", projection_type="scope_summary",
        projection_key="profile",
    )
    assert projection["content"] == "偏好用简洁的中文回答。"
    assert projection["dependencies"] == [{"item_id": item.item_id, "version": item.version}]
    assert "回答时偏好简洁说明" in llm.prompts[0]
    assert await worker.run_once() is False

    async with aiosqlite.connect(repository.path) as db:
        status = await (await db.execute(
            "SELECT status FROM memory_projection_jobs WHERE scope='personal' AND scope_id='user-1'",
        )).fetchone()
    assert status == ("completed",)


@pytest.mark.asyncio
async def test_projection_refresh_race_keeps_newer_refresh_queued(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    item = await repository.create(preference(), request_id="summary-race-fact")

    async def correct_during_summary():
        await repository.correct(
            item.item_id, content="回答时偏好详细说明", reason="user_correction",
            expected_version=1, request_id="summary-race-correction",
        )

    llm = SummaryLLM(before_return=correct_during_summary)
    worker = MemoryWorker(
        repository, None, mode="off",
        projection_builder=MemoryProjectionBuilder(repository, llm),
    )

    assert await worker.run_once()
    async with aiosqlite.connect(repository.path) as db:
        status = await (await db.execute(
            "SELECT status FROM memory_projection_jobs WHERE scope='personal' AND scope_id='user-1'",
        )).fetchone()
    assert status == ("queued",)
    assert await worker.run_once()
    projection = await repository.get_projection(
        scope="personal", scope_id="user-1", projection_type="scope_summary",
        projection_key="profile",
    )
    assert projection["content"] == "偏好用简洁的中文回答。"
    assert projection["dependencies"] == [{"item_id": item.item_id, "version": 2}]
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_projection_refresh_job_recovers_after_repository_restart(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    await repository.create(preference(), request_id="summary-restart-fact")
    claimed = await repository.claim_projection_job()
    assert claimed is not None

    restarted = MemoryRepository(repository.path)
    await restarted.init()
    recovered = await restarted.claim_projection_job()
    assert recovered is not None
    assert recovered["job_id"] == claimed["job_id"]
    assert recovered["attempts"] == 2


@pytest.mark.asyncio
async def test_projection_scans_past_first_page_and_batches_supported_sources(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    older_preferences = [
        await repository.create(preference(
            topic=f"response.style-{index}", content=f"偏好设置 {index}", message_id=f"pref-{index}",
        ), request_id=f"old-pref-{index}")
        for index in range(2)
    ]
    filler_ids = []
    for index in range(200):
        filler = await repository.create(MemoryItemCreate(
            kind="background", scope="personal", scope_id="user-1",
            topic_key=f"background.filler-{index}", content=f"背景 {index}",
        ), request_id=f"summary-filler-{index}")
        filler_ids.append(filler.item_id)
    async with aiosqlite.connect(repository.path) as db:
        await db.execute(
            "UPDATE memory_items SET updated_at='2026-01-02' WHERE item_id IN ("
            + ",".join("?" for _ in filler_ids) + ")", filler_ids,
        )
        await db.execute(
            "UPDATE memory_items SET updated_at='2026-01-01' WHERE item_id IN (?,?)",
            [item.item_id for item in older_preferences],
        )
        await db.commit()

    async def forbidden_per_item_source_lookup(*args, **kwargs):
        raise AssertionError("projection builder must batch source lookups")

    repository.sources = forbidden_per_item_source_lookup
    llm = SummaryLLM()
    worker = MemoryWorker(
        repository, None, mode="off",
        projection_builder=MemoryProjectionBuilder(repository, llm),
    )

    assert await worker.run_once()
    assert all(item.content in llm.prompts[0] for item in older_preferences)


@pytest.mark.asyncio
async def test_projection_rejects_whitespace_only_summary(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    await repository.create(preference(), request_id="blank-summary-fact")
    job = await repository.claim_projection_job()

    with pytest.raises(ValueError, match="memory_projection_invalid_schema"):
        await MemoryProjectionBuilder(repository, SummaryLLM("  \n ")).refresh_profile(job)


@pytest.mark.asyncio
async def test_worker_backfills_active_fact_embeddings_in_bounded_batches(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    saved = [await repository.create(preference(
        topic=f"response.style-{index}", content=f"偏好 {index}", message_id=f"embed-{index}",
    ), request_id=f"embed-{index}") for index in range(3)]

    class EmbeddingClient:
        dimensions = 2

        def __init__(self):
            self.batches = []

        async def embed(self, texts):
            self.batches.append(texts)
            return [[1.0, 0.0] for _ in texts]

    client = EmbeddingClient()
    worker = MemoryWorker(
        repository, None, mode="off", embedding_client=client,
        embedding_model="worker-test-model",
    )

    assert await worker.run_once()
    cached = await repository.embeddings_for(
        [(item.item_id, item.version) for item in saved], model_key="worker-test-model",
    )
    assert set(cached) == {item.item_id for item in saved}
    assert len(client.batches) == 1
    assert len(client.batches[0]) == 3
    assert await worker.run_once() is False


@pytest.mark.asyncio
async def test_worker_embedding_backfill_discards_stale_version_and_retries_current(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    saved = await repository.create(preference(), request_id="embed-versioned")

    class CorrectingEmbeddingClient:
        dimensions = 2
        calls = 0

        async def embed(self, texts):
            self.calls += 1
            if self.calls == 1:
                await repository.correct(
                    saved.item_id, content="回答时偏好详细说明", reason="user_correction",
                    expected_version=1, request_id="embed-correction",
                )
            return [[1.0, 0.0] for _ in texts]

    client = CorrectingEmbeddingClient()
    worker = MemoryWorker(
        repository, None, mode="off", embedding_client=client,
        embedding_model="worker-version-model",
    )

    assert await worker.run_once()
    assert await repository.embeddings_for(
        [(saved.item_id, 1)], model_key="worker-version-model",
    ) == {}
    assert await worker.run_once()
    assert set(await repository.embeddings_for(
        [(saved.item_id, 2)], model_key="worker-version-model",
    )) == {saved.item_id}
    assert client.calls == 2


@pytest.mark.asyncio
async def test_worker_embedding_failure_uses_backoff_then_recovers(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    await repository.create(preference(), request_id="embed-retry")

    class FlakyEmbeddingClient:
        dimensions = 2
        calls = 0

        async def embed(self, texts):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("provider_unavailable")
            return [[1.0, 0.0] for _ in texts]

    client = FlakyEmbeddingClient()
    worker = MemoryWorker(
        repository, None, mode="off", embedding_client=client,
        embedding_model="retry-test-model",
    )

    assert await worker.run_once() is False
    retry_after = worker._embedding_retry_after
    assert retry_after > 0
    state = await repository.embedding_backfill_state(model_key="retry-test-model")
    assert state["failures"] == 1
    assert state["retry_at"] == int(retry_after)

    restarted_repository = MemoryRepository(repository.path)
    await restarted_repository.init()
    restarted_worker = MemoryWorker(
        restarted_repository, None, mode="off", embedding_client=client,
        embedding_model="retry-test-model",
    )
    assert await restarted_worker.run_once() is False
    assert client.calls == 1

    await restarted_repository.record_embedding_backfill_failure(
        model_key="retry-test-model", failures=1, retry_at=0,
    )
    assert await restarted_worker.run_once()
    assert client.calls == 2
    assert restarted_worker._embedding_failures == 0
    assert restarted_worker._embedding_retry_after == 0
    assert await restarted_repository.embedding_backfill_state(model_key="retry-test-model") == {
        "failures": 0, "retry_at": 0,
    }


def test_projection_worker_is_available_when_auto_extraction_is_off(tmp_path):
    from app.application import create_app
    from app.settings import Settings

    app = create_app(Settings(
        _env_file=None, database_path=tmp_path / "agent.db", docs_root=tmp_path,
        memory_enabled=True, memory_auto_mode="off", llm_api_key="test-key",
        desktop_environment_id="user-1",
    ))

    worker = app.state.memory_worker
    assert worker is not None
    assert worker.mode == "off"
    assert worker.extractor is not None
    assert worker.projection_builder is not None
