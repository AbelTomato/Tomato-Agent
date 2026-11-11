import json
from pathlib import Path

import pytest

from app.agent.models import LLMResponse
from app.layered_memory import (
    LayeredMemoryRepository,
    LayeredMemoryWorker,
    _cosine,
    _classify_project,
)


class FakeLLM:
    async def complete(self, messages, tools):
        if "提取" in messages[0].content:
            turns = json.loads(messages[1].content)
            if isinstance(turns, list) and turns:
                first = turns[0]
                content = {"episodes": [{
                    "content": f"用户决定持续推进项目状态：{first.get('user', first.get('development_log', ''))[:30]}",
                    "occurred_at": first.get("occurred_at", "2026-11-11T00:00:00Z"),
                    "turn_ids": [first["turn_id"]],
                }]}
            else:
                content = {"episodes": []}
        else:
            content = {"profile": "用户主要开发 Tomato Agent；偏好直接、可验证的实现。"}
        return LLMResponse(kind="final", content=json.dumps(content, ensure_ascii=False))


class FakeEmbedding:
    dimensions = 2

    async def embed(self, texts):
        return [[1.0, 0.0] if "tomato" in text.lower() or "项目" in text else [0.0, 1.0]
                for text in texts]


@pytest.mark.asyncio
async def test_working_memory_only_queues_after_ten_turns_and_worker_extracts_episodes(tmp_path):
    repository = LayeredMemoryRepository(tmp_path / "agent.db")
    await repository.init()
    for index in range(10):
        await repository.record_turn(
            session_id="session-a", turn_id=f"turn-{index}",
            user_content=f"user {index}", assistant_content=f"assistant {index}",
            occurred_at=f"2026-11-11T00:00:{index:02d}Z",
        )
    assert await repository.claim_job() is None

    await repository.record_turn(
        session_id="session-a", turn_id="turn-10", user_content="Tomato 项目继续采用 Rust",
        assistant_content="已记录项目状态。", occurred_at="2026-11-11T00:00:10Z",
    )
    worker = LayeredMemoryWorker(repository, FakeLLM(), FakeEmbedding(), "test-model")
    assert await worker.run_once() is True
    episodes = await repository.episodes()
    assert len(episodes) == 1
    assert episodes[0]["source_refs"]
    assert episodes[0]["embedding_model"] == "test-model"
    assert episodes[0]["project_key"] is None
    turns = await repository.turns([f"turn-{index}" for index in range(11)])
    assert len(turns) == 11


@pytest.mark.asyncio
async def test_vector_context_is_top_three_at_or_above_fixed_threshold(tmp_path):
    repository = LayeredMemoryRepository(tmp_path / "agent.db")
    await repository.init()
    await repository.save_episodes(
        batch_key="seed",
        episodes=[{
            "content": f"Tomato project event {index}", "occurred_at": "2026-11-11T00:00:00Z",
            "source_refs": [f"log#{index}"],
        } for index in range(5)],
        source_ref="log", vectors=[[1.0, 0.0], [0.9, 0.1], [0.8, 0.2], [0.7, 0.3], [0.0, 1.0]],
        embedding_model="test-model",
    )
    context = await repository.context("Tomato 项目", FakeEmbedding(), "test-model")

    assert len(context["episodes"]) == 3
    assert all(item["similarity"] >= 0.65 for item in context["episodes"])
    assert context["episodes"][0]["similarity"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_vector_context_excludes_episodes_from_other_projects(tmp_path):
    repository = LayeredMemoryRepository(tmp_path / "agent.db")
    await repository.init()
    await repository.save_episodes(
        batch_key="project-a",
        episodes=[{"content": "Tomato project A status", "occurred_at": "2026-11-11",
                   "source_refs": ["a#1"]}], source_ref="a", vectors=[[1.0, 0.0]],
        embedding_model="test-model", project_id="project-a",
    )
    await repository.save_episodes(
        batch_key="project-b",
        episodes=[{"content": "Tomato project B status", "occurred_at": "2026-11-11",
                   "source_refs": ["b#1"]}], source_ref="b", vectors=[[1.0, 0.0]],
        embedding_model="test-model", project_id="project-b",
    )

    context = await repository.context("Tomato 项目", FakeEmbedding(), "test-model", "project-a")

    assert [item["content"] for item in context["episodes"]] == ["Tomato project A status"]


@pytest.mark.asyncio
async def test_vector_context_includes_global_and_current_project_only(tmp_path):
    repository = LayeredMemoryRepository(tmp_path / "agent.db")
    await repository.init()
    await repository.save_episodes(
        batch_key="global",
        episodes=[{"content": "Global preference", "occurred_at": "2026-11-11",
                   "source_refs": ["global#1"]}], source_ref="global", vectors=[[1.0, 0.0]],
        embedding_model="test-model",
    )
    for project_id, label in (("project-a", "Current project"), ("project-b", "Other project")):
        await repository.save_episodes(
            batch_key=project_id,
            episodes=[{"content": label, "occurred_at": "2026-11-11",
                       "source_refs": [project_id]}], source_ref=project_id, vectors=[[1.0, 0.0]],
            embedding_model="test-model", project_id=project_id,
        )

    context = await repository.context(
        "项目状态", FakeEmbedding(), "test-model", project_id="project-a",
    )

    assert {item["content"] for item in context["episodes"]} == {
        "Global preference", "Current project",
    }


@pytest.mark.asyncio
async def test_unscoped_context_does_not_leak_project_episodes(tmp_path):
    repository = LayeredMemoryRepository(tmp_path / "agent.db")
    await repository.init()
    await repository.save_episodes(
        batch_key="project",
        episodes=[{"content": "Project-only state", "occurred_at": "2026-11-11",
                   "source_refs": ["project#1"]}], source_ref="project", vectors=[[1.0, 0.0]],
        embedding_model="test-model", project_key="shopee",
    )

    context = await repository.context("question", FakeEmbedding(), "test-model")

    assert context["episodes"] == []


@pytest.mark.asyncio
async def test_profile_snapshots_are_limited_to_five_and_can_be_restored(tmp_path):
    repository = LayeredMemoryRepository(tmp_path / "agent.db")
    await repository.init()
    for index in range(6):
        profile = await repository.save_profile(f"profile {index}", [f"episode-{index}"])
    async with __import__("aiosqlite").connect(repository.database_path) as db:
        versions = await (await db.execute("SELECT version FROM layered_profiles ORDER BY version")).fetchall()

    assert [row[0] for row in versions] == [2, 3, 4, 5, 6]
    restored = await repository.restore_profile(5)
    assert restored["version"] == 7
    assert restored["content"] == "profile 4"
    assert len(restored["content"]) <= 500
    assert len(await repository.profiles()) == 5


@pytest.mark.asyncio
async def test_idle_session_flush_extracts_and_distills_development_log_events(tmp_path):
    repository = LayeredMemoryRepository(tmp_path / "agent.db")
    await repository.init()
    worker = LayeredMemoryWorker(repository, FakeLLM(), FakeEmbedding(), "test-model")
    assert await repository.enqueue_import(
        source_ref="2026-11-11.md#tomato", text="Tomato Agent memory implementation status.",
        occurred_at="2026-11-11T00:00:00+00:00",
    )
    assert await repository.enqueue_import(
        source_ref="2026-11-11.md#shopee", text="Shopee internship project status.",
        occurred_at="2026-11-11T00:00:00+00:00",
    )
    assert await worker.run_once()
    assert await worker.run_once()
    episodes = await repository.episodes()
    assert len(episodes) == 2
    by_ref = {item["source_refs"][0]: item["project_key"] for item in episodes}
    assert by_ref["2026-11-11.md#tomato"] == "tomato-agent"
    assert by_ref["2026-11-11.md#shopee"] == "shopee"

    profile = await worker.distill_now()
    assert profile["version"] == 1
    assert set(profile["source_episode_ids"]) == {item["episode_id"] for item in episodes}
    assert len(profile["content"]) <= 500


@pytest.mark.asyncio
async def test_short_session_idle_flush_queues_working_memory(tmp_path):
    repository = LayeredMemoryRepository(tmp_path / "agent.db")
    await repository.init()
    await repository.record_turn(
        session_id="short-session", turn_id="turn-a", user_content="重要决定",
        assistant_content="收到", occurred_at="2026-11-11T00:00:00Z",
    )

    assert await repository.queue_idle_sessions(idle_seconds=0) == 1
    job = await repository.claim_job()
    assert job["job_type"] == "summarize_turns"
    assert json.loads(job["payload_json"])["turn_ids"] == ["turn-a"]


def test_development_log_project_classifier_keeps_named_projects_separate():
    assert _classify_project("Tomato Agent 长期记忆开发日志") == "tomato-agent"
    assert _classify_project("Shopee 实习中的服务项目") == "shopee"
    assert _classify_project("蓝桥杯训练项目") == "lanqiao"
    assert _classify_project("个人交互偏好") is None


def test_cosine_handles_zero_and_mismatched_vectors():
    assert _cosine([0, 0], [1, 0]) == 0
    assert _cosine([1, 0], [1]) == 0
