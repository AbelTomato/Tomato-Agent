import asyncio

import pytest

from app.memory.models import MemoryItemCreate, MemorySourceInput
from app.memory.repository import MemoryRepository
from app.memory.retrieval import MemoryRetriever


def item(*, scope="personal", scope_id="user-1", kind="background", topic="work.experience",
         content="长期从事 Python 后端开发"):
    return MemoryItemCreate(
        kind=kind,
        scope=scope,
        scope_id=scope_id,
        topic_key=topic,
        content=content,
        sources=[MemorySourceInput(
            source_system="tomato-desktop", source_installation_id="install-1",
            session_id="session-1", message_id=f"{topic}-source", excerpt=content,
        )],
    )


@pytest.mark.asyncio
async def test_memory_recall_finds_chinese_terms_and_exact_topic_with_valid_sources(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    background = await repository.create(item(), request_id="background")
    preference = await repository.create(item(
        kind="preference", topic="response.language", content="偏好中文回答，简洁直接",
    ), request_id="preference")
    await repository.create(item(
        scope="project", scope_id="project-a", topic="runtime.language", content="这个项目使用 Rust",
    ), request_id="project")
    retriever = MemoryRetriever(repository)

    result = await retriever.recall(
        query="Python 后端开发建议", personal_scope_id="user-1", limit=8, budget_chars=1200,
    )
    assert {entry["item_id"] for entry in result["items"]} == {background.item_id}
    assert {entry["item_id"] for entry in result["core_preferences"]} == {preference.item_id}
    assert result["items"][0]["sources"][0]["role"] == "user"
    assert result["used_chars"] <= result["budget_chars"]

    exact = await retriever.recall(
        query="response.language", personal_scope_id="user-1", limit=8, budget_chars=1200,
    )
    assert exact["core_preferences"][0]["item_id"] == preference.item_id


@pytest.mark.asyncio
async def test_recall_searches_beyond_the_first_two_hundred_items(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    oldest = await repository.create(item(
        topic="background.oldest",
        content="长期维护铯原子钟温度传感器",
    ), request_id="scale-oldest")
    for index in range(200):
        await repository.create(item(
            topic=f"background.archive.{index}",
            content=f"普通历史背景记录编号 {index}",
        ), request_id=f"scale-{index}")

    class BoundedEmbeddingClient:
        dimensions = 2

        def __init__(self):
            self.batch_sizes = []

        async def embed(self, texts):
            self.batch_sizes.append(len(texts))
            return [[1.0, 0.0] if "铯原子钟" in text else [0.0, 1.0] for text in texts]

    embedding_client = BoundedEmbeddingClient()
    result = await MemoryRetriever(
        repository, embedding_client, embedding_model="scale-test",
    ).recall(
        query="铯原子钟温度传感器维护经验",
        personal_scope_id="user-1",
    )

    assert [entry["item_id"] for entry in result["items"]] == [oldest.item_id]
    assert embedding_client.batch_sizes == [1, 64]


@pytest.mark.asyncio
async def test_recall_filters_forgotten_and_wrong_scope_facts_and_respects_budget(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    forgotten = await repository.create(item(content="了解 Redis 缓存设计"), request_id="forgotten")
    await repository.create(item(scope_id="other-user", content="了解 Redis 集群运维"), request_id="other")
    await repository.forget(forgotten.item_id, expected_version=1, request_id="forget")
    result = await MemoryRetriever(repository).recall(
        query="Redis 缓存设计", personal_scope_id="user-1", budget_chars=100,
    )
    assert result["items"] == []
    assert result["used_chars"] == 0


@pytest.mark.asyncio
async def test_recall_is_available_only_when_memory_enabled_and_uses_trusted_personal_scope(tmp_path):
    import httpx

    from app.application import create_app
    from app.settings import Settings

    disabled = create_app(Settings(
        _env_file=None, database_path=tmp_path / "disabled.db", docs_root=tmp_path,
        desktop_environment_id="trusted-user", memory_enabled=False,
    ))
    async with disabled.router.lifespan_context(disabled):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=disabled),
                                     base_url="http://test") as client:
            assert (await client.post("/api/memory/recall", json={"query": "中文"})).status_code == 409

    enabled = create_app(Settings(
        _env_file=None, database_path=tmp_path / "enabled.db", docs_root=tmp_path,
        desktop_environment_id="trusted-user", memory_enabled=True, memory_auto_mode="off",
    ))
    async with enabled.router.lifespan_context(enabled):
        await enabled.state.memory_repository.create(item(
            scope_id="trusted-user", kind="preference", topic="response.language",
            content="偏好中文回答",
        ), request_id="trusted")
        await enabled.state.memory_repository.create(item(
            scope_id="attacker-selected", kind="preference", topic="response.language",
            content="偏好英文回答",
        ), request_id="attacker")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=enabled),
                                     base_url="http://test") as client:
            response = await client.post("/api/memory/recall", json={
                "query": "中文回答", "project_scope_id": "project-a",
                "session_scope_id": "session-a",
            })
            assert response.status_code == 200
            all_facts = response.json()["items"] + response.json()["core_preferences"]
            assert {entry["scope_id"] for entry in all_facts} == {"trusted-user"}


@pytest.mark.asyncio
async def test_optional_embeddings_enable_hybrid_mode_and_provider_failure_falls_back(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    await repository.create(item(
        topic="work.stack", content="长期从事医疗数据算法研究",
    ), request_id="medical-work")
    await repository.create(item(
        kind="preference", topic="response.language", content="偏好 Python 示例",
    ), request_id="python-preference")

    class FakeEmbeddingClient:
        dimensions = 2
        calls = 0

        async def embed(self, texts):
            self.calls += 1
            return [[1.0, 0.0] if "医疗数据算法" in text or "算法研究" in text
                    or "算法研究工作" in text else [0.0, 1.0] for text in texts]

    client = FakeEmbeddingClient()
    hybrid = await MemoryRetriever(
        repository, client, embedding_model="test-embed",
    ).recall(query="算法研究工作", personal_scope_id="user-1")
    assert hybrid["items"][0]["topic_key"] == "work.stack"
    assert all(entry["retrieval_mode"] == "hybrid" for entry in hybrid["items"])
    assert client.calls == 2
    cached = await repository.embeddings_for(
        [(entry["item_id"], entry["version"]) for entry in hybrid["items"]],
        model_key="test-embed",
    )
    assert len(cached) == len(hybrid["items"])

    repeat = await MemoryRetriever(
        repository, client, embedding_model="test-embed",
    ).recall(query="算法研究工作", personal_scope_id="user-1")
    assert repeat["items"][0]["topic_key"] == "work.stack"
    assert client.calls == 3

    class FailingEmbeddingClient:
        async def embed(self, texts):
            raise RuntimeError("embedding unavailable")

    fallback = await MemoryRetriever(
        repository, FailingEmbeddingClient(), embedding_model="test-embed",
    ).recall(query="算法研究工作", personal_scope_id="user-1")
    assert all(entry["retrieval_mode"] == "keyword" for entry in fallback["items"])


@pytest.mark.asyncio
async def test_cold_embedding_requests_do_not_hold_local_vector_recall_gate(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    await repository.create(item(content="长期从事 Python 后端开发"), request_id="cold-a")
    entered = 0
    both_entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingEmbeddingClient:
        dimensions = 2

        async def embed(self, texts):
            nonlocal entered
            entered += 1
            if entered == 2:
                both_entered.set()
            await release.wait()
            return [[1.0, 0.0] for _ in texts]

    retriever = MemoryRetriever(repository, BlockingEmbeddingClient(), embedding_model="cold-test")
    first = asyncio.create_task(retriever.recall(
        query="Python 后端开发", personal_scope_id="user-1",
    ))
    second = asyncio.create_task(retriever.recall(
        query="Python 后端开发", personal_scope_id="user-1",
    ))
    try:
        await asyncio.wait_for(both_entered.wait(), timeout=2)
    finally:
        release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert entered == 3
    assert first_result["items"] and second_result["items"]


@pytest.mark.asyncio
async def test_hybrid_recall_includes_semantic_only_facts_and_respects_scopes(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    await repository.create(item(
        content="每周安排长距离耐力训练", topic="health.routine",
    ), request_id="semantic-personal")
    await repository.create(item(
        scope="project", scope_id="project-a", topic="project.stack",
        content="服务端主要使用 Go",
    ), request_id="project-a")
    await repository.create(item(
        scope="project", scope_id="project-b", topic="project.stack",
        content="服务端主要使用 Go",
    ), request_id="project-b")
    await repository.create(item(
        scope="session", scope_id="session-a", topic="session.routine",
        content="每周安排长距离耐力训练",
    ), request_id="session-a")
    await repository.create(item(
        scope="session", scope_id="session-b", topic="session.routine",
        content="每周安排长距离耐力训练",
    ), request_id="session-b")

    class SemanticEmbeddingClient:
        async def embed(self, texts):
            return [[1.0, 0.0] if "长距离耐力训练" in text or "运动计划" in text
                    or "Go" in text else [0.0, 1.0] for text in texts]

    result = await MemoryRetriever(
        repository, SemanticEmbeddingClient(), embedding_model="test-embed",
    ).recall(query="运动计划", personal_scope_id="user-1", project_scope_id="project-a",
             session_scope_id="session-a")

    assert {entry["scope_id"] for entry in result["items"]} == {"user-1", "project-a", "session-a"}
    assert all(entry["retrieval_mode"] == "hybrid" for entry in result["items"])
    assert all(entry["scope_id"] != "project-b" for entry in result["items"])


@pytest.mark.asyncio
async def test_hybrid_recall_filters_weak_positive_cosine_scores(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    weak_candidate = await repository.create(item(
        content="长期维护古典音乐档案", topic="background.music",
    ), request_id="weak-vector-candidate")

    class WeakSimilarityClient:
        dimensions = 2

        async def embed(self, texts):
            if len(texts) == 1 and texts[0] == "如何规划家庭预算":
                return [[1.0, 0.0]]
            return [[0.3, 0.9539392014] for _ in texts]

    retriever = MemoryRetriever(repository, WeakSimilarityClient(), embedding_model="weak-test")
    result = await retriever.recall(query="如何规划家庭预算", personal_scope_id="user-1")

    assert result["items"] == []
    assert result["retrieval_mode"] == "keyword"
    assert retriever.last_vector_scores[weak_candidate.item_id] == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_core_preferences_and_recalled_facts_share_one_budget(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    await repository.create(item(
        kind="preference", topic="response.style", content="偏好清晰、简短且结构分明的回答",
    ), request_id="preference")
    await repository.create(item(content="熟悉 Python 后端系统维护"), request_id="background")

    result = await MemoryRetriever(repository).recall(
        query="Python 后端维护", personal_scope_id="user-1", budget_chars=300,
    )

    assert result["core_preferences"]
    assert result["items"]
    assert result["used_chars"] <= 300


@pytest.mark.asyncio
async def test_embedding_timeout_falls_back_to_keyword_recall(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    await repository.create(item(content="熟悉 Python 后端系统维护"), request_id="background")

    class TimedOutEmbeddingClient:
        async def embed(self, texts):
            raise TimeoutError("embedding request timed out")

    result = await MemoryRetriever(
        repository, TimedOutEmbeddingClient(), embedding_model="test-embed",
    ).recall(query="Python 后端维护", personal_scope_id="user-1")

    assert result["items"][0]["content"] == "熟悉 Python 后端系统维护"
    assert result["items"][0]["retrieval_mode"] == "keyword"


@pytest.mark.asyncio
async def test_cached_vectors_are_versioned_and_deleted_when_fact_is_forgotten(tmp_path):
    import aiosqlite

    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    saved = await repository.create(item(content="擅长云端系统设计"), request_id="create")
    await repository.store_embeddings(
        model_key="provider|model-a|2", dimensions=2,
        embeddings=[(saved.item_id, saved.version, [1.0, 0.0])],
    )

    corrected = await repository.correct(
        saved.item_id, content="擅长分布式系统设计", reason="correction",
        expected_version=1, request_id="correct",
    )
    async with aiosqlite.connect(repository.path) as db:
        old_vectors = await (await db.execute(
            "SELECT count(*) FROM memory_embeddings WHERE item_id=? AND version=1", (saved.item_id,),
        )).fetchone()
    assert old_vectors[0] == 0
    assert await repository.embeddings_for(
        [(saved.item_id, saved.version), (corrected.item_id, corrected.version)],
        model_key="provider|model-a|2",
    ) == {}
    await repository.store_embeddings(
        model_key="provider|model-b|2", dimensions=2,
        embeddings=[(corrected.item_id, corrected.version, [0.0, 1.0])],
    )
    assert await repository.embeddings_for(
        [(corrected.item_id, corrected.version)], model_key="provider|model-a|2",
    ) == {}
    assert await repository.embeddings_for(
        [(corrected.item_id, corrected.version)], model_key="provider|model-b|2",
    ) == {saved.item_id: [0.0, 1.0]}

    await repository.forget(saved.item_id, expected_version=2, request_id="forget")
    async with aiosqlite.connect(repository.path) as db:
        rows = await (await db.execute("SELECT count(*) FROM memory_embeddings")).fetchone()
    assert rows[0] == 0


@pytest.mark.asyncio
async def test_recall_discards_snapshot_if_memory_changes_during_embedding(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    saved = await repository.create(item(content="熟悉 Python 后端服务"), request_id="create")

    class MutatingEmbeddingClient:
        async def embed(self, texts):
            await repository.forget(saved.item_id, expected_version=1, request_id="concurrent-forget")
            return [[1.0, 0.0] for _ in texts]

    result = await MemoryRetriever(
        repository, MutatingEmbeddingClient(), embedding_model="test-embed",
    ).recall(query="Python 后端", personal_scope_id="user-1")

    assert result["items"] == []
    assert result["core_preferences"] == []
    assert result["retrieval_mode"] == "stale_snapshot_discarded"


def test_memory_sources_cannot_use_assistant_as_fact_evidence():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MemorySourceInput(
            source_system="tomato-desktop", source_installation_id="install-1",
            session_id="session-1", message_id="assistant-message", role="assistant",
        )
