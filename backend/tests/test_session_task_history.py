from uuid import UUID

import pytest

from test_unified_tasks_api import api, body


async def test_readonly_legacy_history_and_linked_domain_dedup(tmp_path):
    async with api(tmp_path) as (app, client, session):
        repo = app.state.session_repository
        legacy = await repo.create_run(UUID(session))
        await repo.append_event(UUID(session), legacy, "user_message", {"content": "old"})
        await repo.append_event(UUID(session), legacy, "assistant_message", {"content": "answer"})
        await repo.update_run(legacy, "completed", state={}, loop_count=1)
        writing = await app.state.writing_repository.create_task(UUID(session), "old writing")
        created = await client.post(f"/api/sessions/{session}/tasks", json=body())
        assert created.status_code == 202
        run_id = created.json()["run_id"]
        lease = await app.state.execution_repository.claim(UUID(run_id), "test", lease_seconds=30)
        snapshot = await app.state.execution_repository.get_snapshot(UUID(run_id))
        await app.state.execution_repository.link_stage(lease, snapshot.model_copy(update={"domain_run_id": str(legacy)}))
        path = f"/api/sessions/{session}/tasks"
        first = await client.get(path)
        assert first.status_code == 200
        assert first.json() == (await client.get(path)).json()
        items = first.json()["tasks"]
        assert len(items) == 2
        assert {item["run_id"] for item in items} == {run_id, str(writing.task_id)}
        assert next(item for item in items if item["run_id"] == run_id)["messages"][1]["content"] == "answer"
        assert next(item for item in items if item["legacy"])["kind"] == "writing"
        page = (await client.get(path, params={"limit": 1, "offset": 1})).json()
        assert page["tasks"] == items[1:2]
        assert (await client.get(path, params={"offset": -1})).status_code == 422
        assert (await client.get(path, params={"limit": 0})).status_code == 422


async def test_sessions_default_workspace_persistence_and_clear(tmp_path):
    async with api(tmp_path) as (app, client, session):
        project = tmp_path / "project"
        project.mkdir()
        (project / "test_example.py").write_text("def test_ok(): assert True\n")
        registered = await client.post("/api/workspaces", json={"path": str(project), "pytest_paths": ["test_example.py"]})
        workspace = registered.json()["workspace_id"]
        route = f"/api/sessions/{session}/workspace"
        response = await client.post(route, json={"workspace_id": workspace})
        assert response.status_code == 200
        assert response.json()["default_workspace_id"] == workspace
        await app.state.session_repository.init()
        listed = (await client.get("/api/sessions")).json()["sessions"]
        assert listed[0]["default_workspace_id"] == workspace
        available = (await client.get("/api/workspaces")).json()["workspaces"]
        assert any(item["workspace_id"] == workspace and item["available"] for item in available)
        assert (await client.post(route, json={"workspace_id": None})).json()["default_workspace_id"] is None
        assert (await client.get("/api/sessions", params={"limit": 0})).status_code == 422


async def test_cross_session_domain_link_is_rejected(tmp_path):
    async with api(tmp_path) as (app, client, session):
        other = await app.state.session_repository.create_session()
        domain = await app.state.session_repository.create_run(other)
        created = await client.post(f"/api/sessions/{session}/tasks", json=body())
        assert created.status_code == 202
        run_id = created.json()["run_id"]
        lease = await app.state.execution_repository.claim(UUID(run_id), "test", lease_seconds=30)
        snapshot = await app.state.execution_repository.get_snapshot(UUID(run_id))
        await app.state.execution_repository.link_stage(lease, snapshot.model_copy(update={"domain_run_id": str(domain)}))
        assert (await client.get(f"/api/tasks/{run_id}")).status_code == 409
        assert (await client.get(f"/api/sessions/{session}/tasks")).status_code == 409


async def test_tied_creation_order_and_legacy_knowledge_are_readonly(tmp_path, monkeypatch):
    import aiosqlite
    from unittest.mock import AsyncMock
    async with api(tmp_path) as (app, client, session):
        repo = app.state.session_repository
        domain = await repo.create_run(UUID(session))
        await repo.append_event(UUID(session), domain, "user_message", {"content": "question"})
        await repo.append_event(UUID(session), domain, "assistant_message", {"answer": "material insufficient", "citations": []})
        await repo.update_run(domain, "completed")
        created = (await client.post(f"/api/sessions/{session}/tasks", json=body())).json()
        async with aiosqlite.connect(repo.path) as db:
            await db.execute("UPDATE runs SET created_at='2026-10-11T00:00:00+00:00'")
            await db.execute("UPDATE task_runs SET created_at='2026-10-11T00:00:00+00:00'")
            await db.commit()
        execute = AsyncMock(side_effect=AssertionError("GET must never generate"))
        monkeypatch.setattr(app.state.task_service, "execute", execute)
        path = f"/api/sessions/{session}/tasks"
        items = (await client.get(path)).json()["tasks"]
        assert [item["run_id"] for item in items] == sorted([str(domain), created["run_id"]])
        legacy = next(item for item in items if item["legacy"])
        assert legacy["kind"] == "knowledge" and legacy["result"]["citations"] == []
        assert (await client.get(path)).json()["tasks"] == items
        execute.assert_not_called()


async def test_session_old_schema_upgrade_preserves_metadata(tmp_path):
    import aiosqlite
    from uuid import uuid4
    from app.sessions.repository import SessionRepository
    path = tmp_path / "legacy.db"
    session = uuid4()
    async with aiosqlite.connect(path) as db:
        await db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, status TEXT NOT NULL, metadata TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        await db.execute("INSERT INTO sessions VALUES (?, 'active', '{\"title\": \"legacy\"}', '2026-10-11T00:00:00+00:00', '2026-10-11T00:00:00+00:00')", (str(session),))
        await db.commit()
    repo = SessionRepository(path)
    await repo.init()
    await repo.init()
    record = await repo.get_session(session)
    assert record.metadata == {"title": "legacy"} and record.default_workspace_id is None
    assert await repo.create_session()