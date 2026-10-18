import json
from pathlib import Path

import pytest

from app.llm.compatible_client import _REQUEST_CORRELATION
from app.assistant.strategy import ASSISTANT_DECISION_RESPONSE_FORMAT
from tests.fixtures.scripted_runtime import (
    FINAL_ANSWER, STAGE_MESSAGE, ScriptedRuntimeClient, decision,
)
from test_assistant_api import assistant_api, register, request


async def test_scripted_runtime_clarification_tool_finish_and_history(tmp_path):
    async with assistant_api(tmp_path) as (app, client, session, _):
        provider = ScriptedRuntimeClient()
        app.state.task_service.assistant.llm = provider
        workspace = await register(app, tmp_path)
        route = f"/api/sessions/{session}/tasks"
        body = request(workspace_id=workspace.workspace_id)
        created = await client.post(route, json=body)
        assert created.status_code == 202
        run_id = created.json()["run_id"]
        assert (await client.post(route, json=body)).json()["run_id"] == run_id
        assert await app.state.execution_worker.run_once()
        waiting = (await client.get(f"/api/tasks/{run_id}")).json()
        assert waiting["status"] == "waiting"
        assert waiting["pending_action"]["action_type"] == "clarification"
        assert provider.calls == 2
        before = (await client.get(f"/api/tasks/{run_id}/events")).json()
        assert any(e["event_type"] == "assistant.message" and
                   e["payload"]["detail"]["data"]["text"] == STAGE_MESSAGE for e in before["events"])
        assert not any(e["event_type"] == "tool.started" for e in before["events"])
        assert (await client.get(route)).json()["tasks"][0]["pending_action"] == waiting["pending_action"]
        action = {"action": "provide_input", "action_id": waiting["pending_action"]["action_id"],
                  "expected_version": waiting["version"], "client_request_id": "script-input",
                  "payload": {"message": "cache.py"}}
        actions = f"/api/tasks/{run_id}/actions"
        assert (await client.post(actions, json={**action, "expected_version": 999})).status_code == 409
        assert provider.calls == 2
        assert (await client.post(actions, json=action)).status_code == 200
        assert (await client.post(actions, json=action)).status_code == 409
        assert await app.state.execution_worker.run_once()
        final = (await client.get(f"/api/tasks/{run_id}")).json()
        assert final["status"] == "completed", final
        assert final["run_id"] == run_id and final["pending_action"] is None
        assert final["result"]["answer"] == FINAL_ANSWER
        assert provider.calls == 5
        after = (await client.get(f"/api/tasks/{run_id}/events",
                                  params={"after": before["next_cursor"]})).json()
        events = before["events"] + after["events"]
        sequences = [e["sequence"] for e in events]
        assert sequences == sorted(set(sequences))
        started = [e for e in events if e["event_type"] == "tool.started"]
        succeeded = [e for e in events if e["event_type"] == "tool.succeeded"]
        assert len(started) == len(succeeded) == 1
        for key in ("invocation_id", "step_id"):
            assert started[0]["payload"][key] and started[0]["payload"][key] == succeeded[0]["payload"][key]
        history = (await client.get(route)).json()["tasks"]
        assert len(history) == 1 and history[0]["result"] == final["result"]
        assert (tmp_path / "project" / "cache.py").read_text() == "CACHE_TTL = 60\n"
        assert [o["decision_type"] for o in provider.observations] == [
            "message", "ask_user", "message", "call_tool", "finish"]


async def test_scripted_runtime_cancel_does_not_consume_remaining_script(tmp_path):
    async with assistant_api(tmp_path) as (app, client, session, _):
        provider = ScriptedRuntimeClient()
        app.state.task_service.assistant.llm = provider
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        waiting = (await client.get(f"/api/tasks/{run_id}")).json()
        response = await client.post(f"/api/tasks/{run_id}/actions", json={
            "action": "cancel", "action_id": "cancel", "expected_version": waiting["version"],
            "client_request_id": "cancel", "payload": {}})
        assert response.json()["status"] == "cancelled"
        assert not await app.state.execution_worker.run_once()
        assert provider.calls == 2


async def test_scripted_client_is_finite_and_requires_protocol_and_run():
    provider = ScriptedRuntimeClient([decision("message", {"text": "phase"})])
    with pytest.raises(ValueError, match="decision_protocol"):
        await provider.complete([], [])
    with pytest.raises(ValueError, match="run_identity"):
        await provider.complete([], [], response_format=ASSISTANT_DECISION_RESPONSE_FORMAT)
    token = _REQUEST_CORRELATION.set({"run_id": "one"})
    try:
        response = await provider.complete([], [], response_format=ASSISTANT_DECISION_RESPONSE_FORMAT)
        assert json.loads(response.content)["decision"]["type"] == "message"
        with pytest.raises(RuntimeError, match="exhausted"):
            await provider.complete([], [], response_format=ASSISTANT_DECISION_RESPONSE_FORMAT)
    finally:
        _REQUEST_CORRELATION.reset(token)
    assert provider.calls == 1


async def test_scripted_client_rejects_invalid_decision():
    provider = ScriptedRuntimeClient([decision("finish", {"result": {"unexpected": True}})])
    token = _REQUEST_CORRELATION.set({"run_id": "invalid"})
    try:
        with pytest.raises(ValueError):
            await provider.complete([], [], response_format=ASSISTANT_DECISION_RESPONSE_FORMAT)
    finally:
        _REQUEST_CORRELATION.reset(token)
    assert provider.calls == 0


async def test_scripted_client_isolates_progress_per_run():
    provider = ScriptedRuntimeClient([decision("message", {"text": "phase"})])
    for run_id in ("first", "second"):
        token = _REQUEST_CORRELATION.set({"run_id": run_id})
        try:
            await provider.complete([], [], response_format=ASSISTANT_DECISION_RESPONSE_FORMAT)
        finally:
            _REQUEST_CORRELATION.reset(token)
    assert provider.calls_by_run == {"first": 1, "second": 1}


def test_scripted_server_rejects_business_database_directory(monkeypatch):
    from tests.fixtures.scripted_runtime_server import create_scripted_app

    monkeypatch.setenv("SCRIPTED_RUNTIME_ROOT", str(Path(__file__).parents[1] / "data"))
    with pytest.raises(ValueError, match="isolated acceptance directory"):
        create_scripted_app()