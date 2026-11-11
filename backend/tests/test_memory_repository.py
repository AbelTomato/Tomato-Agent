import pytest
import aiosqlite

from app.memory.models import MemoryItemCreate, MemorySourceInput
from app.memory.repository import MemoryConflictError, MemoryRepository


def memory_item(*, scope_id="user-1", content="喜欢中文回答"):
    return MemoryItemCreate(
        kind="preference",
        scope="personal",
        scope_id=scope_id,
        topic_key="response.language",
        content=content,
        sources=[MemorySourceInput(
            source_system="tomato-desktop",
            source_installation_id="install-1",
            session_id="session-1",
            message_id="message-1",
            source_revision=1,
            excerpt="记住我喜欢中文回答",
        )],
    )


@pytest.mark.asyncio
async def test_memory_schema_is_idempotent_and_versions_survive_restart(tmp_path):
    database = tmp_path / "engine" / "agent.db"
    repository = MemoryRepository(database)
    await repository.init()
    await repository.init()

    async with aiosqlite.connect(database) as db:
        migrations = await db.execute_fetchall("SELECT version FROM memory_schema_migrations ORDER BY version")
    assert migrations[-1][0] == 6

    created = await repository.create(memory_item(), request_id="create-1")
    repeated = await repository.create(memory_item(), request_id="create-1")
    assert repeated.item_id == created.item_id
    assert repeated.version == 1
    assert await repository.generation() == 1
    assert await repository.sources(created.item_id, 1) == [{
        "source_system": "tomato-desktop",
        "source_installation_id": "install-1",
        "session_id": "session-1",
        "message_id": "message-1",
        "source_revision": 1,
        "role": "user",
        "message_at": None,
        "excerpt": "记住我喜欢中文回答",
        "locator": None,
        "status": "active",
        "evidence_role": "support",
    }]

    corrected = await repository.correct(
        created.item_id,
        content="默认使用简体中文回答",
        reason="user_correction",
        expected_version=1,
        request_id="correct-1",
    )
    assert corrected.version == 2
    assert corrected.content == "默认使用简体中文回答"
    assert len(await repository.sources(created.item_id, 2)) == 1
    with pytest.raises(MemoryConflictError):
        await repository.correct(
            created.item_id, content="stale", reason="user_correction",
            expected_version=1, request_id="correct-stale",
        )
    with pytest.raises(ValueError, match="idempotency_key_reused"):
        await repository.correct(
            created.item_id, content="different retry", reason="user_correction",
            expected_version=1, request_id="correct-1",
        )

    restarted = MemoryRepository(database)
    await restarted.init()
    persisted = await restarted.get(created.item_id)
    assert persisted == corrected
    assert await restarted.generation() == 2


@pytest.mark.asyncio
async def test_memory_listing_is_scope_isolated_and_only_returns_active_items(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    personal = await repository.create(memory_item(scope_id="user-a"), request_id="a")
    await repository.create(memory_item(scope_id="user-b"), request_id="b")

    assert [item.item_id for item in await repository.list_items(
        scope="personal", scope_id="user-a",
    )] == [personal.item_id]
    assert await repository.list_items(scope="project", scope_id="user-a") == []


@pytest.mark.asyncio
async def test_memory_listing_detects_a_missing_current_version(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    saved = await repository.create(memory_item(), request_id="missing-current-version")
    async with aiosqlite.connect(repository.path) as db:
        await db.execute(
            "DELETE FROM memory_versions WHERE item_id=? AND version=?",
            (saved.item_id, saved.version),
        )
        await db.commit()

    with pytest.raises(RuntimeError, match="memory_current_version_missing"):
        await repository.list_items(scope="personal", scope_id="user-1")


@pytest.mark.asyncio
async def test_sources_for_items_batches_versions_and_preserves_multiple_sources(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    item_versions = []
    for index in range(301):
        sources = [] if index == 300 else [MemorySourceInput(
            source_system="tomato-desktop", source_installation_id="install-1",
            session_id="session-1", message_id=f"message-{index}", excerpt=f"source {index}",
        )]
        if index == 0:
            sources.append(MemorySourceInput(
                source_system="tomato-desktop", source_installation_id="install-1",
                session_id="session-2", message_id="message-0-second", excerpt="second source",
            ))
        created = await repository.create(MemoryItemCreate(
            kind="background", scope="personal", scope_id="user-1",
            topic_key=f"background.item-{index}", content=f"Fact {index}", sources=sources,
        ), request_id=f"batch-source-{index}")
        item_versions.append((created.item_id, created.version))

    result = await repository.sources_for_items(item_versions)

    assert len(result) == 300
    assert {source["message_id"] for source in result[item_versions[0]]} == {
        "message-0", "message-0-second",
    }
    assert result[item_versions[1]][0]["status"] == "active"
    assert item_versions[-1] not in result
    assert await repository.sources_for_items([]) == {}


@pytest.mark.asyncio
async def test_memory_context_trace_hashes_query_and_forget_removes_fact_reference(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    item = await repository.create(memory_item(), request_id="context-item")
    context_id = await repository.record_context(
        run_id="run-1", session_id="session-1", step_id="step-1", scope_id="user-1",
        generation=await repository.generation(), query="my private current question",
        retrieval_mode="keyword", used_chars=94, used_tokens=34,
        items=[{"item_id": item.item_id, "version": item.version,
                "selection_reason": "core_preference", "rank": 1,
                "used_chars": 44, "used_tokens": 16, "source_available": True}],
    )
    contexts = await repository.list_contexts(run_id="run-1")
    assert contexts[0]["context_id"] == context_id
    assert contexts[0]["query_hash"] != "my private current question"
    assert contexts[0]["items"][0]["item_id"] == item.item_id

    await repository.forget(item.item_id, expected_version=1, request_id="forget-context-item")
    contexts = await repository.list_contexts(run_id="run-1")
    assert contexts == []


@pytest.mark.asyncio
async def test_memory_projection_is_bound_to_current_supported_fact_version(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    item = await repository.create(memory_item(), request_id="projection-item")
    generation = await repository.generation()
    projection = await repository.save_projection(
        scope="personal", scope_id="user-1", projection_type="topic_summary",
        projection_key="response", content="用户偏好用中文回答。", expected_generation=generation,
        dependencies=[{"item_id": item.item_id, "version": item.version}],
    )

    assert (await repository.get_projection(
        scope="personal", scope_id="user-1", projection_type="topic_summary",
        projection_key="response",
    ))["content"] == "用户偏好用中文回答。"

    corrected = await repository.correct(
        item.item_id, content="默认使用简体中文回答", reason="user_correction",
        expected_version=1, request_id="projection-correction",
    )
    assert corrected.version == 2
    assert await repository.get_projection(
        scope="personal", scope_id="user-1", projection_type="topic_summary",
        projection_key="response",
    ) is None
    async with aiosqlite.connect(repository.path) as db:
        stale = await (await db.execute(
            "SELECT content,status FROM memory_projections WHERE projection_id=?",
            (projection["projection_id"],),
        )).fetchone()
    assert stale == ("", "stale")

    refreshed = await repository.save_projection(
        scope="personal", scope_id="user-1", projection_type="topic_summary",
        projection_key="response", content="用户偏好使用简体中文回答。",
        expected_generation=await repository.generation(),
        dependencies=[{"item_id": item.item_id, "version": corrected.version}],
    )
    await repository.delete_source({
        "source_system": "tomato-desktop", "source_installation_id": "install-1",
        "session_id": "session-1", "message_id": "message-1", "source_revision": 1,
    }, request_id="projection-source-delete")
    assert await repository.get_projection(
        scope="personal", scope_id="user-1", projection_type="topic_summary",
        projection_key="response",
    ) is None
    async with aiosqlite.connect(repository.path) as db:
        stale = await (await db.execute(
            "SELECT content,status FROM memory_projections WHERE projection_id=?",
            (refreshed["projection_id"],),
        )).fetchone()
    assert stale == ("", "stale")

    forgotten_item = await repository.create(MemoryItemCreate(
        kind="habit", scope="personal", scope_id="user-1", topic_key="work.planning",
        content="习惯每周制定计划", sources=[MemorySourceInput(
            source_system="tomato-desktop", source_installation_id="install-1",
            session_id="session-2", message_id="planning-source", excerpt="每周计划",
        )],
    ), request_id="projection-forget-item")
    forgotten_projection = await repository.save_projection(
        scope="personal", scope_id="user-1", projection_type="topic_summary",
        projection_key="work", content="用户习惯每周计划。",
        expected_generation=await repository.generation(),
        dependencies=[{"item_id": forgotten_item.item_id, "version": forgotten_item.version}],
    )
    await repository.forget(
        forgotten_item.item_id, expected_version=1, request_id="projection-forget",
    )
    async with aiosqlite.connect(repository.path) as db:
        stale = await (await db.execute(
            "SELECT content,status FROM memory_projections WHERE projection_id=?",
            (forgotten_projection["projection_id"],),
        )).fetchone()
    assert stale == ("", "stale")


@pytest.mark.asyncio
async def test_projection_write_rejects_stale_generation_and_cross_scope_dependencies(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    item = await repository.create(memory_item(), request_id="projection-conflict-item")
    with pytest.raises(MemoryConflictError, match="memory_generation_conflict"):
        await repository.save_projection(
            scope="personal", scope_id="user-1", projection_type="topic_summary",
            projection_key="response", content="stale summary", expected_generation=0,
            dependencies=[{"item_id": item.item_id, "version": item.version}],
        )
    with pytest.raises(MemoryConflictError, match="memory_projection_dependency_stale"):
        await repository.save_projection(
            scope="personal", scope_id="other-user", projection_type="topic_summary",
            projection_key="response", content="cross-scope summary",
            expected_generation=await repository.generation(),
            dependencies=[{"item_id": item.item_id, "version": item.version}],
        )


@pytest.mark.asyncio
async def test_memory_api_supports_manual_crud_and_optimistic_versioning(tmp_path):
    import httpx

    from app.application import create_app
    from app.settings import Settings

    app = create_app(Settings(
        _env_file=None,
        database_path=tmp_path / "agent.db",
        docs_root=tmp_path,
        task_worker_enabled=False,
        desktop_environment_id="user-1",
    ))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            created = await client.post("/api/memory/items?request_id=create-api", json={
                "kind": "preference", "scope": "personal", "scope_id": "user-1",
                "topic_key": "response.language", "content": "喜欢中文回答",
                "sources": [{
                    "source_system": "tomato-desktop",
                    "source_installation_id": "install-1",
                    "session_id": "session-1",
                    "message_id": "message-1",
                    "excerpt": "记住我喜欢中文回答",
                }],
            })
            assert created.status_code == 200, created.text
            item = created.json()
            repeated = await client.post("/api/memory/items?request_id=create-api", json={
                "kind": "preference", "scope": "personal", "scope_id": "user-1",
                "topic_key": "response.language", "content": "喜欢中文回答",
                "sources": [{
                    "source_system": "tomato-desktop",
                    "source_installation_id": "install-1",
                    "session_id": "session-1",
                    "message_id": "message-1",
                    "excerpt": "记住我喜欢中文回答",
                }],
            })
            assert repeated.json()["item_id"] == item["item_id"]
            reused = await client.post("/api/memory/items?request_id=create-api", json={
                "kind": "preference", "scope": "personal", "scope_id": "user-1",
                "topic_key": "response.language", "content": "different payload",
            })
            assert reused.status_code == 409
            listed = await client.get("/api/memory/items", params={
                "scope": "personal", "scope_id": "user-1",
            })
            assert [value["item_id"] for value in listed.json()["items"]] == [item["item_id"]]
            detail = await client.get(f"/api/memory/items/{item['item_id']}")
            assert detail.json()["sources"][0]["excerpt"] == "记住我喜欢中文回答"
            corrected = await client.patch(
                f"/api/memory/items/{item['item_id']}?request_id=correct-api",
                json={"content": "默认用简体中文回答", "expected_version": 1},
            )
            assert corrected.status_code == 200
            assert corrected.json()["version"] == 2
            historical = await client.get(f"/api/memory/items/{item['item_id']}/versions/1")
            assert historical.status_code == 200
            assert historical.json()["content"] == "喜欢中文回答"
            assert historical.json()["sources"][0]["excerpt"] == "记住我喜欢中文回答"
            stale = await client.patch(
                f"/api/memory/items/{item['item_id']}?request_id=correct-stale-api",
                json={"content": "stale", "expected_version": 1},
            )
            assert stale.status_code == 409
            forgotten = await client.post(
                f"/api/memory/items/{item['item_id']}/forget?request_id=forget-after-version-read",
                json={"expected_version": 2},
            )
            assert forgotten.status_code == 200
            assert (await client.get(f"/api/memory/items/{item['item_id']}/versions/1")).status_code == 404
            forbidden = await client.get("/api/memory/items", params={
                "scope": "personal", "scope_id": "other-user",
            })
            assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_forget_clears_fact_and_evidence_and_blocks_old_source(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    item = await repository.create(memory_item(), request_id="create-forget")
    forgotten = await repository.forget(
        item.item_id, expected_version=1, request_id="forget-1",
    )
    assert forgotten["status"] == "forgotten"
    assert await repository.get(item.item_id) is None
    assert await repository.sources(item.item_id, 1) == []

    repeated = await repository.forget(
        item.item_id, expected_version=1, request_id="forget-1",
    )
    assert repeated == forgotten
    with pytest.raises(ValueError, match="idempotency_key_reused"):
        await repository.forget(item.item_id, expected_version=2, request_id="forget-1")

    async with aiosqlite.connect(repository.path) as db:
        source = await (await db.execute(
            "SELECT excerpt, locator FROM memory_sources"
        )).fetchone()
        suppression = await (await db.execute(
            "SELECT scope, scope_id, topic_key, source_key_hash FROM memory_suppressions"
        )).fetchall()
    assert source == (None, None)
    assert ("personal", "user-1", "response.language", "") in suppression
    assert any(row[3] for row in suppression)


@pytest.mark.asyncio
async def test_forgetting_one_fact_preserves_source_still_supporting_another(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    source_data = memory_item().sources
    first = await repository.create(memory_item(), request_id="shared-source-first")
    second = await repository.create(
        MemoryItemCreate(kind="habit", scope="personal", scope_id="user-1",
                         topic_key="response.detail", content="偏好细节解释",
                         sources=source_data),
        request_id="shared-source-second",
    )
    await repository.forget(first.item_id, expected_version=1, request_id="forget-shared")
    remaining = await repository.sources(second.item_id, 1)
    assert remaining[0]["excerpt"] == "记住我喜欢中文回答"


@pytest.mark.asyncio
async def test_source_deletion_removes_unsupported_fact_but_keeps_other_supported_fact(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    item = await repository.create(memory_item(), request_id="create-source-delete")
    result = await repository.delete_source({
        "source_system": "tomato-desktop",
        "source_installation_id": "install-1",
        "session_id": "session-1",
        "message_id": "message-1",
        "source_revision": 1,
    }, request_id="delete-source-1")
    assert result["status"] == "deleted"
    assert await repository.get(item.item_id) is None
    async with aiosqlite.connect(repository.path) as db:
        source = await (await db.execute(
            "SELECT status, excerpt FROM memory_sources"
        )).fetchone()
        suppressions = await (await db.execute(
            "SELECT scope, scope_id, topic_key FROM memory_suppressions"
        )).fetchall()
    assert source == ("deleted", None)
    assert ("personal", "user-1", "response.language") in suppressions
    with pytest.raises(ValueError, match="memory_suppressed"):
        await repository.create(
            memory_item().model_copy(update={"confirmation": "automatic"}), request_id="auto-resurrect",
        )
    explicitly_readded = await repository.create(
        memory_item(), request_id="explicit-readd",
    )
    assert explicitly_readded.status == "active"


@pytest.mark.asyncio
async def test_early_source_tombstone_blocks_delayed_automatic_extraction(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    identity = {
        "source_system": "tomato-desktop", "source_installation_id": "install-1",
        "session_id": "session-1", "message_id": "message-1", "source_revision": 1,
    }
    await repository.delete_source(identity, request_id="delete-before-ingest")
    with pytest.raises(ValueError, match="memory_suppressed"):
        await repository.create(
            memory_item().model_copy(update={"confirmation": "automatic"}),
            request_id="late-automatic-extraction",
        )
    deliberate = await repository.create(memory_item(), request_id="explicit-remember-again")
    assert deliberate.status == "active"
    assert (await repository.sources(deliberate.item_id, 1))[0]["status"] == "active"


@pytest.mark.asyncio
async def test_clear_requires_current_generation_and_removes_all_personal_content(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    first = await repository.create(memory_item(), request_id="clear-first")
    second = await repository.create(
        memory_item().model_copy(update={"topic_key": "response.format", "content": "简洁"}),
        request_id="clear-second",
    )
    generation = await repository.generation()
    with pytest.raises(MemoryConflictError, match="memory_generation_conflict"):
        await repository.clear(scope="personal", scope_id="user-1",
                               expected_generation=generation - 1, request_id="clear-stale")
    result = await repository.clear(scope="personal", scope_id="user-1",
                                    expected_generation=generation, request_id="clear-now")
    assert result == {"status": "cleared", "generation": generation + 1, "forgotten_count": 2}
    assert await repository.get(first.item_id) is None
    assert await repository.get(second.item_id) is None
    with pytest.raises(ValueError, match="memory_suppressed"):
        await repository.create(memory_item().model_copy(update={"confirmation": "automatic"}),
                                request_id="clear-resurrect")


@pytest.mark.asyncio
async def test_memory_delete_and_clear_http_contracts(tmp_path):
    import httpx

    from app.application import create_app
    from app.settings import Settings

    app = create_app(Settings(_env_file=None, database_path=tmp_path / "agent.db",
                              docs_root=tmp_path, task_worker_enabled=False,
                              desktop_environment_id="user-1"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            item = await client.post("/api/memory/items?request_id=http-create", json={
                "kind": "preference", "scope": "personal", "scope_id": "user-1",
                "topic_key": "response.language", "content": "喜欢中文",
                "sources": [{"source_system": "desktop", "source_installation_id": "i1",
                             "session_id": "s1", "message_id": "m1", "excerpt": "记住中文"}],
            })
            ident = item.json()["item_id"]
            exported = await client.get("/api/memory/export")
            assert exported.status_code == 200
            assert exported.json()["scope"] == "personal"
            assert exported.json()["items"][0]["item_id"] == ident
            assert exported.json()["items"][0]["sources"][0]["excerpt"] == "记住中文"
            forgotten = await client.post(
                f"/api/memory/items/{ident}/forget?request_id=http-forget",
                json={"expected_version": 1},
            )
            assert forgotten.status_code == 200
            assert (await client.get(f"/api/memory/items/{ident}")).status_code == 404

            second = await client.post("/api/memory/items?request_id=http-create-2", json={
                "kind": "preference", "scope": "personal", "scope_id": "user-1",
                "topic_key": "response.format", "content": "简洁",
            })
            status = await client.get("/api/memory/status")
            cleared = await client.post("/api/memory/clear?request_id=http-clear", json={
                "scope": "personal", "scope_id": "user-1",
                "expected_generation": status.json()["generation"],
            })
            assert cleared.status_code == 200
            assert (await client.get(f"/api/memory/items/{second.json()['item_id']}")).status_code == 404

            deleted = await client.post("/api/memory/sources/delete?request_id=http-source-delete", json={
                "source_system": "desktop", "source_installation_id": "i1",
                "session_id": "s1", "message_id": "m1", "source_revision": 1,
            })
            assert deleted.status_code == 200


@pytest.mark.asyncio
async def test_list_items_for_recall_returns_only_active_items_in_requested_scope(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    active = await repository.create(memory_item(), request_id="recall-scope-active")
    other_scope = memory_item().model_copy(update={"scope_id": "other-user"})
    await repository.create(other_scope, request_id="recall-scope-other")
    forgotten = await repository.create(
        memory_item(content="睡眠偏好"), request_id="recall-scope-forgotten",
    )
    await repository.forget(
        forgotten.item_id, expected_version=1, request_id="recall-scope-forget",
    )

    items = await repository.list_items_for_recall(scope="personal", scope_id="user-1")

    assert [item.item_id for item in items] == [active.item_id]
    assert items[0].content == active.content


@pytest.mark.asyncio
async def test_sources_for_recall_uses_large_item_sets_and_preserves_missing_sources(tmp_path):
    repository = MemoryRepository(tmp_path / "agent.db")
    await repository.init()
    items = [
        await repository.create(
            memory_item(content=f"偏好 {index}"), request_id=f"recall-source-batch-{index}",
        )
        for index in range(305)
    ]
    result = await repository.sources_for_recall(
        [(item.item_id, item.version) for item in items]
        + [("missing-item", 1)],
    )

    assert len(result) == 305
    assert all(len(result[(item.item_id, item.version)]) == 1 for item in items)
    assert ("missing-item", 1) not in result
