from contextlib import asynccontextmanager

import httpx
import pytest

from app.agent.models import LLMResponse, ToolCall
from test_application_lifecycle import make_app


class Provider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0
        self.messages = []

    async def complete(self, messages, tools):
        self.calls += 1
        self.messages.append(messages)
        return next(self.responses)


@asynccontextmanager
async def assistant_api(tmp_path, responses=(), enabled=True):
    app = make_app(tmp_path, assistant_enabled=enabled, workspace_allowed_roots=[tmp_path])
    async with app.router.lifespan_context(app):
        app.state.task_service.enabled = True
        app.state.task_service.model_available = True
        provider = Provider(responses)
        app.state.task_service.assistant.llm = provider
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            session = (await client.post("/api/sessions", json={})).json()["session_id"]
            yield app, client, session, provider


def request(**changes):
    return {"kind": "assistant", "message": "Explain the project", "client_request_id": "first", **changes}


async def test_assistant_http_worker_replay_and_history(tmp_path):
    async with assistant_api(tmp_path, [LLMResponse(kind="final", content="hello")]) as (app, client, session, provider):
        route = f"/api/sessions/{session}/tasks"
        response = await client.post(route, json=request())
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        assert (await client.post(route, json=request())).json()["run_id"] == run_id
        assert (await client.post(route, json=request(message="changed"))).status_code == 409
        assert await app.state.execution_worker.run_once()
        current = (await client.get(f"/api/tasks/{run_id}")).json()
        assert current["status"] == "completed"
        assert current["result"]["answer"] == "hello"
        assert current["result"]["validation_status"] == "not_required"
        assert provider.calls == 1
        snapshot = await app.state.execution_repository.get_snapshot(run_id)
        assert snapshot.snapshot_type == "assistant" and snapshot.reserved_model_calls == 1
        assert len((await client.get(route)).json()["tasks"]) == 1
        assert (await client.get(f"/api/tasks/{run_id}/events")).status_code == 200


async def test_disabled_assistant_does_not_create_run(tmp_path):
    async with assistant_api(tmp_path, enabled=False) as (app, client, session, _):
        response = await client.post(f"/api/sessions/{session}/tasks", json=request())
        assert response.status_code == 503 and response.json()["detail"]["code"] == "assistant_disabled"
        assert await app.state.run_repository.list_for_session(session) == []


async def register(app, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "tests").mkdir()
    (root / "cache.py").write_text("CACHE_TTL = 60\n")
    return await app.state.workspace_service.register_original(root, ["tests"])


async def test_workspace_wait_bind_once_resume_and_budget(tmp_path):
    responses = [LLMResponse(kind="tool_call", tool_call=ToolCall(name="read_file", arguments={"path": "cache.py"})),
                 LLMResponse(kind="tool_call", tool_call=ToolCall(name="read_file", arguments={"path": "cache.py"})),
                 LLMResponse(kind="final", content="TTL is 60")]
    async with assistant_api(tmp_path, responses) as (app, client, session, provider):
        workspace = await register(app, tmp_path)
        created = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()
        run_id = created["run_id"]
        assert await app.state.execution_worker.run_once()
        waiting = (await client.get(f"/api/tasks/{run_id}")).json()
        assert waiting["status"] == "waiting"
        assert waiting["pending_action"]["action_type"] == "workspace_binding"
        import aiosqlite
        async with aiosqlite.connect(app.state.execution_repository.path) as db:
            operations = await db.execute_fetchall("SELECT id FROM execution_operations WHERE run_id=?", (run_id,))
        assert operations == []
        action = {"action": "bind_workspace", "action_id": waiting["pending_action"]["action_id"],
                  "expected_version": waiting["version"], "client_request_id": "bind",
                  "payload": {"workspace_id": workspace.workspace_id}}
        route = f"/api/tasks/{run_id}/actions"
        assert (await client.post(route, json={**action, "expected_version": 999})).status_code == 409
        assert (await client.post(route, json=action)).status_code == 200
        assert (await client.post(route, json=action)).status_code == 200
        assert (await client.post(route, json={**action, "payload": {"workspace_id": "missing"}})).status_code == 409
        assert await app.state.execution_worker.run_once()
        final = (await client.get(f"/api/tasks/{run_id}")).json()
        assert final["status"] == "completed" and final["workspace_id"] == workspace.workspace_id
        assert final["result"]["answer"] == "TTL is 60"
        assert provider.calls == 3
        assert (await app.state.execution_repository.get_snapshot(run_id)).reserved_model_calls == 3
        assert (await client.post(route, json={**action, "client_request_id": "again", "expected_version": final["version"]})).status_code == 409


@pytest.mark.parametrize("workspace", ["missing", "busy"])
async def test_submit_rejects_invalid_or_busy_workspace(tmp_path, workspace):
    async with assistant_api(tmp_path) as (app, client, session, _):
        registered = await register(app, tmp_path)
        workspace_id = registered.workspace_id if workspace == "busy" else "missing"
        if workspace == "busy":
            other = (await client.post("/api/sessions", json={})).json()["session_id"]
            assert (await client.post(f"/api/sessions/{other}/tasks", json=request(workspace_id=workspace_id))).status_code == 202
        response = await client.post(f"/api/sessions/{session}/tasks", json=request(workspace_id=workspace_id))
        assert response.status_code in {404, 409, 422}
        assert await app.state.run_repository.list_for_session(session) == []


@pytest.mark.parametrize("failure", ["missing", "busy", "session", "operation"])
async def test_binding_transaction_rejects_invalid_facts(tmp_path, failure):
    import aiosqlite
    from uuid import uuid4
    responses = [LLMResponse(kind="tool_call", tool_call=ToolCall(name="read_file", arguments={"path": "cache.py"}))]
    async with assistant_api(tmp_path, responses) as (app, client, session, _):
        workspace = await register(app, tmp_path)
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        waiting = (await client.get(f"/api/tasks/{run_id}")).json()
        workspace_id = workspace.workspace_id
        if failure == "missing":
            workspace_id = str(uuid4())
        elif failure == "busy":
            other = (await client.post("/api/sessions", json={})).json()["session_id"]
            assert (await client.post(f"/api/sessions/{other}/tasks", json=request(workspace_id=workspace_id))).status_code == 202
        elif failure == "session":
            snapshot = await app.state.execution_repository.get_snapshot(run_id)
            corrupted = snapshot.model_copy(update={"session_id": uuid4()})
            async with aiosqlite.connect(app.state.execution_repository.path) as db:
                await db.execute("UPDATE task_run_checkpoints SET state=? WHERE run_id=?", (corrupted.model_dump_json(), run_id))
                await db.commit()
        else:
            async with aiosqlite.connect(app.state.execution_repository.path) as db:
                await db.execute("INSERT INTO execution_operations(id,run_id,logical_index,kind,tool_name,input_digest,input_payload,recovery_class,status) "
                    "VALUES (?,?,0,'tool','read_file','digest','{}','read_only','prepared')", (str(uuid4()), run_id))
                await db.commit()
        action = {"action": "bind_workspace", "action_id": waiting["pending_action"]["action_id"],
                  "expected_version": waiting["version"], "client_request_id": "bad-bind", "payload": {"workspace_id": workspace_id}}
        response = await client.post(f"/api/tasks/{run_id}/actions", json=action)
        assert response.status_code in {404, 409, 422}
        current = await app.state.run_repository.get_run(run_id)
        assert current.workspace_id is None and current.status == "waiting"