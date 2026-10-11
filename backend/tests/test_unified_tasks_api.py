from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest

from test_application_lifecycle import make_app
from test_writing_full_flow import rag_database_path


@asynccontextmanager
async def api(tmp_path):
    app = make_app(tmp_path, workspace_allowed_roots=[tmp_path])
    async with app.router.lifespan_context(app):
        app.state.task_service.enabled = True
        app.state.task_service.model_available = True
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            session = (await client.post("/api/sessions", json={})).json()["session_id"]
            yield app, client, session


def body(**values):
    return {"kind": "chat", "message": "hello", "client_request_id": "first", **values}


async def test_submit_replay_conflict_and_cancel_version(tmp_path):
    async with api(tmp_path) as (app, client, session):
        path = f"/api/sessions/{session}/tasks"
        first = await client.post(path, json=body())
        assert first.status_code == 202
        run = first.json()
        assert run["session_id"] == session and run["kind"] == "chat"
        assert run["status"] == "queued" and run["workspace_id"] is None
        assert (await client.post(path, json=body())).json()["run_id"] == run["run_id"]
        conflict = await client.post(path, json=body(message="changed"))
        assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "request_conflict"
        assert (await client.post(path, json=body(client_request_id="second"))).status_code == 409
        action = {"action_id": "cancel", "action": "cancel", "expected_version": 99,
                  "client_request_id": "cancel-1", "payload": {}}
        route = f"/api/tasks/{run['run_id']}/actions"
        assert (await client.post(route, json=action)).status_code == 409
        action["expected_version"] = run["version"]
        assert (await client.post(route, json=action)).json()["status"] == "cancelled"
        assert (await client.get(f"/api/tasks/{run['run_id']}")).json()["capabilities"] == []


@pytest.mark.parametrize("values", [{"kind": "code_task"}, {"options": {"limit": 1}},
    {"kind": "knowledge", "options": {"limit": True}}, {"kind": "code"},
    {"extra": True}, {"message": "  "}])
async def test_invalid_requests(tmp_path, values):
    async with api(tmp_path) as (_, client, session):
        assert (await client.post(f"/api/sessions/{session}/tasks", json=body(**values))).status_code == 422


@pytest.mark.parametrize("field,code", [("enabled", "worker_disabled"), ("model_available", "model_unconfigured")])
async def test_unavailable_service(tmp_path, field, code):
    async with api(tmp_path) as (app, client, session):
        setattr(app.state.task_service, field, False)
        result = await client.post(f"/api/sessions/{session}/tasks", json=body())
        assert result.status_code == 503 and result.json()["detail"] == {"code": code}
        assert await app.state.run_repository.list_for_session(session) == []


async def test_events_cursor_and_ids_are_checked(tmp_path):
    async with api(tmp_path) as (app, client, session):
        result = await client.post(f"/api/sessions/{session}/tasks", json=body())
        assert result.status_code == 202
        run_id = result.json()["run_id"]
        for index in range(3):
            await app.state.run_repository.append_event(run_id, "test", {"index": index})
        path = f"/api/tasks/{run_id}/events"
        first = (await client.get(path, params={"limit": 2})).json()
        assert len(first["events"]) == 2
        second = (await client.get(path, params={"after": first["next_cursor"]})).json()
        assert all(e["sequence"] > first["next_cursor"] for e in second["events"])
        assert (await client.get(path, params={"after": -1})).status_code == 422
        assert (await client.get(path, params={"limit": 0})).status_code == 422
        assert (await client.get(f"/api/tasks/{uuid4()}")).status_code == 404
        assert (await client.get("/api/tasks/not-a-uuid")).status_code == 400
        orphan = await app.state.run_repository.create_run("code_task", {}, "legacy")
        assert (await client.get(f"/api/tasks/{orphan.id}")).status_code == 404


async def test_unknown_workspace_and_session(tmp_path):
    async with api(tmp_path) as (_, client, session):
        result = await client.post(f"/api/sessions/{session}/tasks", json=body(kind="code", workspace_id=str(uuid4())))
        assert result.status_code == 404
        assert (await client.post(f"/api/sessions/{uuid4()}/tasks", json=body())).status_code == 404


async def test_code_binding_result_and_backend_errors(tmp_path):
    from app.agent.policies import CodeTaskCapabilityProfile
    from app.tools.code_tests import RunTestsTool, TestTargetRegistry
    from test_original_code_task import Sandbox, execute
    async with api(tmp_path) as (app, client, session):
        workspaces = []
        for name in ["one", "two"]:
            project = tmp_path / name
            project.mkdir()
            (project / "app.py").write_text("value = 0\n")
            (project / "test_app.py").write_text("def test_ok(): assert True\n")
            result = await client.post("/api/workspaces", json={"path": str(project), "pytest_paths": ["test_app.py"]})
            workspaces.append(result.json()["workspace_id"])
        service = app.state.code_task_service
        original = workspaces[0]
        path = f"/api/sessions/{session}/tasks"
        req = body(kind="code", workspace_id=original)
        service.test_tool = None
        unavailable = await client.post(path, json=req)
        assert unavailable.status_code == 503
        service.test_tool = RunTestsTool(TestTargetRegistry([]), Sandbox(), service.workspace_service,
                                        service.artifact_service, service.run_repository)
        service.capability_profile_factory = lambda root: CodeTaskCapabilityProfile(
            allowed_paths=(str(root),), sandbox_backend="docker")
        await client.post(f"/api/sessions/{session}/workspace", json={"workspace_id": workspaces[1]})
        created = await client.post(path, json=req)
        assert created.status_code == 202 and created.json()["workspace_id"] == original
        run_id = created.json()["run_id"]
        run = await service.run_repository.get_run(run_id)
        await execute(service, run, value=1)
        payload = (await client.get(f"/api/tasks/{run_id}")).json()
        assert payload["result"]["test_results"][0]["passed"] is True
        assert payload["result"]["diff_artifact"] and payload["result"]["changed_files"] == ["app.py"]
        assert (tmp_path / "two" / "app.py").read_text() == "value = 0\n"
        assert (await client.post(path, json=req)).json()["run_id"] == run_id


@pytest.mark.parametrize("action,payload", [("save_draft", {"key": "client"}),
    ("continue_recovery", {"confirmed": "yes"}), ("cancel", {"outline": {}}),
    ("anything", {})])
async def test_action_payload_is_typed(tmp_path, action, payload):
    async with api(tmp_path) as (_, client, session):
        run = (await client.post(f"/api/sessions/{session}/tasks", json=body())).json()
        response = await client.post(f"/api/tasks/{run['run_id']}/actions", json={
            "action_id": "a", "action": action, "expected_version": run["version"],
            "client_request_id": "a", "payload": payload})
        assert response.status_code == 422


async def test_writing_http_confirmation_replay_and_save(tmp_path, rag_database_path):
    from test_task_adapters import writing_setup, action
    from app.application import create_app
    from app.settings import Settings
    deps, session, service, llm = await writing_setup(tmp_path, rag_database_path)
    app = create_app(Settings(_env_file=None, database_path=tmp_path / "unused.db", llm_api_key=""))
    for name, value in vars(deps).items():
        setattr(app.state, name, value)
    app.state.writing_repository = service.repository
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/sessions/{session}/tasks", json=body(kind="writing", message="SETEX expiration"))
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        await deps.execution_worker.run_once()
        run = await deps.run_repository.get_run(run_id)
        req = action(run, "confirm_outline", outline=run.result["outline"]).model_dump(mode="json")
        path = f"/api/tasks/{run_id}/actions"
        stale = await client.post(path, json={**req, "expected_version": 99})
        assert stale.status_code == 409
        assert (await client.post(path, json=req)).json()["status"] == "queued"
        assert (await client.post(path, json=req)).status_code == 200
        await deps.execution_worker.run_once()
        run = await deps.run_repository.get_run(run_id)
        assert "save_draft" in (await client.get(f"/api/tasks/{run_id}")).json()["capabilities"]
        save = action(run, "save_draft").model_dump(mode="json")
        assert (await client.post(path, json=save)).status_code == 200
        await deps.execution_worker.run_once()
        assert (await client.post(path, json=req)).json()["status"] == "completed"
        assert (await client.post(path, json={**req, "payload": {"outline": {}}})).status_code == 409
        assert llm.outline_calls == llm.draft_calls == 1


async def test_code_waiting_has_versioned_recovery_action(tmp_path, monkeypatch):
    from uuid import UUID
    from app.agent.models import CodeTaskRequest
    from unittest.mock import AsyncMock
    async with api(tmp_path) as (app, client, session):
        service = app.state.code_task_service
        orphan = await service.create(CodeTaskRequest(task="test"))
        # Reuse the compatible code snapshot while explicitly associating this test Run.
        import aiosqlite
        async with aiosqlite.connect(service.run_repository.path) as db:
            await db.execute("UPDATE task_runs SET session_id=?, task_type='code' WHERE id=?", (session, str(orphan.id)))
            await db.commit()
        lease = await service.execution_repository.claim(orphan.id, "owner", lease_seconds=30)
        await service.execution_repository.finish(lease, "waiting")
        payload = (await client.get(f"/api/tasks/{orphan.id}")).json()
        assert payload["pending_action"]["reason"] == "recovery_required"
        assert "continue_recovery" in payload["capabilities"]
        recover = AsyncMock(return_value={"status": "waiting"})
        monkeypatch.setattr(service, "recover", recover)
        req = {"action": "continue_recovery", "action_id": "wrong", "expected_version": payload["version"],
               "client_request_id": "recover", "payload": {"confirmed": True}}
        path = f"/api/tasks/{orphan.id}/actions"
        assert (await client.post(path, json=req)).status_code == 409
        recover.assert_not_called()
        req["action_id"] = payload["pending_action"]["action_id"]
        req["payload"] = {"confirmed": False}
        assert (await client.post(path, json=req)).status_code == 409
        recover.assert_not_called()
        req["payload"] = {"confirmed": True}
        assert (await client.post(path, json=req)).status_code == 200
        recover.assert_awaited_once()
