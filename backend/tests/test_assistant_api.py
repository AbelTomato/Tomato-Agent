from contextlib import asynccontextmanager
import json

import httpx
import pytest

from app.agent.models import LLMResponse
from test_application_lifecycle import make_app
from app.tasks.models import TaskActionRequest


class Provider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0
        self.messages = []
        self.response_formats = []

    async def complete(self, messages, tools, *, response_format=None):
        self.calls += 1
        self.messages.append(messages)
        self.response_formats.append(response_format)
        assert tools == []
        response = next(self.responses)
        if response_format is None:
            return response
        decision_payload = json.loads(response.content)
        decision_payload = {"decision": decision_payload}
        if decision_payload["decision"]["type"] == "call_tool":
            decision_payload["decision"]["payload"]["arguments"] = json.dumps(
                decision_payload["decision"]["payload"]["arguments"]
            )
        if decision_payload["decision"]["type"] == "finish":
            result = decision_payload["decision"]["payload"]["result"]
            if "test_results" in result:
                result["test_results"] = [json.dumps(item) for item in result["test_results"]]
        return response.model_copy(update={"content": json.dumps(decision_payload)})


@asynccontextmanager
async def assistant_api(tmp_path, responses=(), enabled=True, memory_enabled=False):
    app = make_app(tmp_path, assistant_enabled=enabled, workspace_allowed_roots=[tmp_path],
                   memory_enabled=memory_enabled)
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


def test_project_read_authorization_action_requires_an_opaque_grant():
    action = TaskActionRequest(
        action_id="read-approval",
        action="authorize_project_read",
        expected_version=2,
        client_request_id="approval-1",
        payload={"grant": "opaque-host-issued-grant"},
    )
    assert action.payload == {"grant": "opaque-host-issued-grant"}
    with pytest.raises(ValueError):
        TaskActionRequest(
            action_id="read-approval",
            action="authorize_project_read",
            expected_version=2,
            client_request_id="approval-2",
            payload={"grant": "", "project_id": "model-selected"},
        )


def decision(kind, payload):
    return LLMResponse(kind="final", content=json.dumps(
        {"protocol_version": 1, "type": kind, "payload": payload}))


async def test_non_runtime_llm_consumers_keep_default_request_format(tmp_path):
    from app.llm.compatible_client import OpenAICompatibleClient
    from app.agent.models import Message

    captured = {}

    async def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "legacy"}}]})

    client = OpenAICompatibleClient(api_key="test-key", model="test-model",
        transport=httpx.MockTransport(handler))
    await client.complete([Message(role="user", content="hello")], [])
    assert "response_format" not in captured


async def test_assistant_http_worker_replay_and_history(tmp_path):
    async with assistant_api(tmp_path, [decision("finish", {"result": {"answer": "hello"}})]) as (app, client, session, provider):
        route = f"/api/sessions/{session}/tasks"
        response = await client.post(route, json=request())
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        visible = await app.state.session_repository.list_visible_messages(session)
        assert [(message.role, message.content) for message in visible] == [("user", "Explain the project")]
        assert (await client.post(route, json=request())).json()["run_id"] == run_id
        assert (await client.post(route, json=request(message="changed"))).status_code == 409
        assert await app.state.execution_worker.run_once()
        current = (await client.get(f"/api/tasks/{run_id}")).json()
        assert current["status"] == "completed"
        assert current["result"]["answer"] == "hello"
        assert current["result"]["validation_status"] == "not_required"
        visible = await app.state.session_repository.list_visible_messages(session)
        assert [(message.role, message.content) for message in visible] == [
            ("user", "Explain the project"), ("assistant", "hello")]
        assert provider.calls == 1
        assert provider.response_formats[0]["type"] == "json_schema"
        assert provider.response_formats[0]["json_schema"]["strict"] is True
        snapshot = await app.state.execution_repository.get_snapshot(run_id)
        assert snapshot.snapshot_type == "assistant" and snapshot.reserved_model_calls == 1
        assert len((await client.get(route)).json()["tasks"]) == 1
        assert (await client.get(f"/api/tasks/{run_id}/events")).status_code == 200


async def test_disabled_assistant_does_not_create_run(tmp_path):
    async with assistant_api(tmp_path, enabled=False) as (app, client, session, _):
        response = await client.post(f"/api/sessions/{session}/tasks", json=request())
        assert response.status_code == 503 and response.json()["detail"]["code"] == "assistant_disabled"
        assert await app.state.run_repository.list_for_session(session) == []


async def test_question_answer_is_explicit_single_consumption_and_same_run(tmp_path):
    responses = [decision("ask_user", {"question": "Choose", "choices": ["A", "B"], "allow_free_text": True}),
        decision("finish", {"result": {"answer": "continued"}})]
    async with assistant_api(tmp_path, responses) as (app, client, session, provider):
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        waiting = (await client.get(f"/api/tasks/{run_id}")).json()
        action = {"action": "provide_input", "action_id": waiting["pending_action"]["action_id"],
            "expected_version": waiting["version"], "client_request_id": "answer", "payload": {"choice": "A"}}
        assert (await client.post(f"/api/sessions/{session}/tasks", json=request(message="ordinary", client_request_id="ordinary"))).status_code == 409
        route = f"/api/tasks/{run_id}/actions"
        assert (await client.post(route, json={**action, "action": "approve_action", "payload": {}})).status_code == 409
        assert (await client.post(route, json={**action, "payload": {"choice": "invalid"}})).status_code == 422
        assert (await client.post(route, json=action)).status_code == 200
        assert (await client.post(route, json=action)).status_code == 409
        assert await app.state.execution_worker.run_once()
        completed = (await client.get(f"/api/tasks/{run_id}")).json()
        assert completed["status"] == "completed" and completed["run_id"] == run_id
        assert provider.calls == 2
        events = (await client.get(f"/api/tasks/{run_id}/events")).json()["events"]
        assert len([e for e in events if e["event_type"] == "assistant.resumed"]) == 1


async def test_project_read_waits_for_explicit_run_grant_before_broker_access(tmp_path):
    class Broker:
        def __init__(self):
            self.calls = []

        async def call(self, operation, payload, **identity):
            self.calls.append((operation, payload, identity))
            if operation == "revoke_run":
                return {"revoked": True}
            return {"path": "README.md", "content": "project contents", "bytes": 16}

    responses = [
        decision("call_tool", {"name": "read_file", "contract_version": "1", "arguments": {"path": "README.md"}}),
        decision("call_tool", {"name": "read_file", "contract_version": "1", "arguments": {"path": "README.md"}}),
        decision("finish", {"result": {"answer": "The project contents are available."}}),
    ]
    async with assistant_api(tmp_path, responses) as (app, client, session, _):
        await app.state.session_repository.set_project(session, "project-1")
        broker = Broker()
        app.state.task_service.assistant.registry.get("read_file").bridge = broker
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        waiting = (await client.get(f"/api/tasks/{run_id}")).json()
        assert waiting["status"] == "waiting"
        assert waiting["pending_action"]["action_type"] == "project_read"
        assert broker.calls == []

        action = {"action_id": waiting["pending_action"]["action_id"],
            "action": "authorize_project_read", "expected_version": waiting["version"],
            "client_request_id": "grant", "payload": {"grant": "host-issued-grant"}}
        assert (await client.post(f"/api/tasks/{run_id}/actions", json=action)).status_code == 200
        assert await app.state.execution_worker.run_once()
        completed = (await client.get(f"/api/tasks/{run_id}")).json()
        assert completed["status"] == "completed"
        snapshot = await app.state.execution_repository.get_snapshot(run_id)
        assert [call[0] for call in broker.calls] == ["read_file", "revoke_run"]
        assert broker.calls[0][2]["run_id"] == run_id
        assert snapshot.harness_state["structured_state"]["project_read_grant"] == "host-issued-grant"
        assert all("host-issued-grant" not in message.content for message in snapshot.messages)


async def test_project_read_rejection_is_persisted_and_blocks_retry(tmp_path):
    class Broker:
        def __init__(self):
            self.calls = []

        async def call(self, operation, payload, **identity):
            self.calls.append(operation)
            return {"path": "README.md", "content": "should not be read", "bytes": 18}

    responses = [
        decision("call_tool", {"name": "read_file", "contract_version": "1", "arguments": {"path": "README.md"}}),
        decision("call_tool", {"name": "read_file", "contract_version": "1", "arguments": {"path": "README.md"}}),
        decision("finish", {"result": {"answer": "I will continue without reading the project."}}),
    ]
    async with assistant_api(tmp_path, responses) as (app, client, session, provider):
        await app.state.session_repository.set_project(session, "project-1")
        broker = Broker()
        app.state.task_service.assistant.registry.get("read_file").bridge = broker
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        waiting = (await client.get(f"/api/tasks/{run_id}")).json()
        action = {"action_id": waiting["pending_action"]["action_id"],
            "action": "reject_action", "expected_version": waiting["version"],
            "client_request_id": "reject-project-read", "payload": {}}
        assert (await client.post(f"/api/tasks/{run_id}/actions", json=action)).status_code == 200
        assert (await client.post(f"/api/tasks/{run_id}/actions", json=action)).status_code == 409
        assert await app.state.execution_worker.run_once()

        completed = (await client.get(f"/api/tasks/{run_id}")).json()
        snapshot = await app.state.execution_repository.get_snapshot(run_id)
        events = (await client.get(f"/api/tasks/{run_id}/events")).json()["events"]
        assert completed["status"] == "completed"
        assert provider.calls == 3
        assert broker.calls == []
        assert snapshot.harness_state["structured_state"]["blocked_capabilities"]["read_file"] == "user_rejected"
        assert snapshot.harness_state["structured_state"]["rejected_actions"] == [action["action_id"]]
        assert [event["event_type"] for event in events].count("assistant.action") == 1


async def test_rejected_changeset_proposal_leaves_project_bytes_unchanged(tmp_path):
    import hashlib

    project = tmp_path / "project"
    project.mkdir()
    project_file = project / "README.md"
    project_file.write_bytes(b"before\n")

    class Broker:
        def __init__(self):
            self.calls = []

        async def call(self, operation, payload, **identity):
            self.calls.append(operation)
            if operation == "read_file":
                return {"path": "README.md", "content": "before\n", "bytes": 7}
            return {"revoked": True}

    responses = [
        decision("call_tool", {"name": "read_file", "contract_version": "1",
            "arguments": {"path": "README.md"}}),
        decision("call_tool", {"name": "read_file", "contract_version": "1",
            "arguments": {"path": "README.md"}}),
        decision("call_tool", {"name": "propose_changeset", "contract_version": "1",
            "arguments": {"changes": [{
                "relative_path": "README.md",
                "operation": "modify",
                "expected_old_sha256": hashlib.sha256(b"before\n").hexdigest(),
                "base_content": "before\n",
                "content": "after\n",
                "summary": "Update project notes",
            }]}}),
        decision("finish", {"result": {"answer": "The proposed change was declined."}}),
    ]
    async with assistant_api(tmp_path, responses) as (app, client, session, provider):
        await app.state.session_repository.set_project(session, "project-1")
        broker = Broker()
        app.state.task_service.assistant.registry.get("read_file").bridge = broker
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        waiting_read = (await client.get(f"/api/tasks/{run_id}")).json()
        grant_action = {"action_id": waiting_read["pending_action"]["action_id"],
            "action": "authorize_project_read", "expected_version": waiting_read["version"],
            "client_request_id": "grant-before-changeset", "payload": {"grant": "host-issued-grant"}}
        assert (await client.post(f"/api/tasks/{run_id}/actions", json=grant_action)).status_code == 200
        assert await app.state.execution_worker.run_once()

        waiting_changeset = (await client.get(f"/api/tasks/{run_id}")).json()
        assert waiting_changeset["pending_action"]["action_type"] == "changeset_approval"
        reject_action = {"action_id": waiting_changeset["pending_action"]["action_id"],
            "action": "reject_action", "expected_version": waiting_changeset["version"],
            "client_request_id": "reject-changeset", "payload": {}}
        assert (await client.post(f"/api/tasks/{run_id}/actions", json=reject_action)).status_code == 200
        assert await app.state.execution_worker.run_once()

        completed = (await client.get(f"/api/tasks/{run_id}")).json()
        assert completed["status"] == "completed"
        assert provider.calls == 4
        assert project_file.read_bytes() == b"before\n"
        assert "apply_changeset" not in broker.calls


async def test_applied_changeset_receipt_resumes_only_the_bound_runtime_action(tmp_path):
    import hashlib

    project = tmp_path / "project"
    project.mkdir()
    project_file = project / "README.md"
    project_file.write_bytes(b"before\n")

    class Broker:
        def __init__(self):
            self.calls = []

        async def call(self, operation, payload, **identity):
            self.calls.append(operation)
            if operation == "read_file":
                return {"path": "README.md", "content": "before\n", "bytes": 7}
            return {"revoked": True}

    responses = [
        decision("call_tool", {"name": "read_file", "contract_version": "1",
            "arguments": {"path": "README.md"}}),
        decision("call_tool", {"name": "read_file", "contract_version": "1",
            "arguments": {"path": "README.md"}}),
        decision("call_tool", {"name": "propose_changeset", "contract_version": "1",
            "arguments": {"changes": [{
                "relative_path": "README.md",
                "operation": "modify",
                "expected_old_sha256": hashlib.sha256(b"before\n").hexdigest(),
                "base_content": "before\n",
                "content": "after\n",
                "summary": "Update project notes",
            }]}}),
        decision("finish", {"result": {"answer": "The approved change was applied."}}),
    ]
    async with assistant_api(tmp_path, responses) as (app, client, session, provider):
        await app.state.session_repository.set_project(session, "project-1")
        broker = Broker()
        app.state.task_service.assistant.registry.get("read_file").bridge = broker
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        waiting_read = (await client.get(f"/api/tasks/{run_id}")).json()
        grant_action = {"action_id": waiting_read["pending_action"]["action_id"],
            "action": "authorize_project_read", "expected_version": waiting_read["version"],
            "client_request_id": "grant-before-approved-changeset", "payload": {"grant": "host-issued-grant"}}
        assert (await client.post(f"/api/tasks/{run_id}/actions", json=grant_action)).status_code == 200
        assert await app.state.execution_worker.run_once()

        waiting_changeset = (await client.get(f"/api/tasks/{run_id}")).json()
        pending = waiting_changeset["pending_action"]
        changeset = pending["display_data"]["changeset"]
        assert pending["action_type"] == "changeset_approval"
        assert changeset["changes"][0]["relative_path"] == "README.md"
        assert (await client.get(f"/api/tasks/{run_id}/desktop-project-read-grant?session_id={session}")).status_code == 404
        app.state.desktop_connection_installed = True
        recovered_grant = await client.get(
            f"/api/tasks/{run_id}/desktop-project-read-grant?session_id={session}"
        )
        assert recovered_grant.status_code == 200
        assert recovered_grant.json() == {"grant": "host-issued-grant", "project_id": "project-1"}
        assert (await client.get(
            f"/api/tasks/{run_id}/desktop-project-read-grant?session_id=another-session"
        )).status_code == 404
        project_file.write_bytes(b"after\n")
        receipt = {"schema_version": 1, "change_id": changeset["change_id"],
            "digest": pending["display_data"]["digest"], "status": "completed",
            "files": [{"relative_path": "README.md", "status": "applied", "error": None}]}
        approve_action = {"action_id": pending["action_id"], "action": "approve_action",
            "expected_version": waiting_changeset["version"], "client_request_id": "approve-change-set",
            "payload": {"commit_receipt": receipt}}
        route = f"/api/tasks/{run_id}/actions"
        applied = await client.post(route, json=approve_action)
        assert applied.status_code == 200
        assert applied.json()["pending_action"] is None
        assert (await client.post(route, json=approve_action)).status_code == 409
        assert await app.state.execution_worker.run_once()

        completed = (await client.get(f"/api/tasks/{run_id}")).json()
        assert completed["status"] == "completed"
        assert completed["result"]["answer"] == "The approved change was applied."
        assert project_file.read_bytes() == b"after\n"
        assert broker.calls == ["read_file", "revoke_run"]
        assert provider.calls == 4


async def test_cancelled_project_read_rpc_is_audited_without_committing_late_content(tmp_path):
    import asyncio

    class Broker:
        def __init__(self):
            self.calls = []
            self.read_started = asyncio.Event()
            self.release_read = asyncio.Event()

        async def call(self, operation, payload, **identity):
            self.calls.append(operation)
            if operation == "revoke_run":
                return {"revoked": True}
            self.read_started.set()
            await self.release_read.wait()
            return {"path": "README.md", "content": "late private contents", "bytes": 21}

    responses = [
        decision("call_tool", {"name": "read_file", "contract_version": "1", "arguments": {"path": "README.md"}}),
        decision("call_tool", {"name": "read_file", "contract_version": "1", "arguments": {"path": "README.md"}}),
        decision("finish", {"result": {"answer": "This must not be committed."}}),
    ]
    async with assistant_api(tmp_path, responses) as (app, client, session, provider):
        await app.state.session_repository.set_project(session, "project-1")
        broker = Broker()
        app.state.task_service.assistant.registry.get("read_file").bridge = broker
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        waiting = (await client.get(f"/api/tasks/{run_id}")).json()
        grant_action = {"action_id": waiting["pending_action"]["action_id"],
            "action": "authorize_project_read", "expected_version": waiting["version"],
            "client_request_id": "grant-before-cancel", "payload": {"grant": "host-issued-grant"}}
        assert (await client.post(f"/api/tasks/{run_id}/actions", json=grant_action)).status_code == 200

        worker = asyncio.create_task(app.state.execution_worker.run_once())
        await asyncio.wait_for(broker.read_started.wait(), timeout=2)
        running = (await client.get(f"/api/tasks/{run_id}")).json()
        cancel_action = {"action_id": "cancel-read", "action": "cancel",
            "expected_version": running["version"], "client_request_id": "cancel-read", "payload": {}}
        cancelled = await client.post(f"/api/tasks/{run_id}/actions", json=cancel_action)
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"

        broker.release_read.set()
        assert await asyncio.wait_for(worker, timeout=2)

        final = (await client.get(f"/api/tasks/{run_id}")).json()
        snapshot = await app.state.execution_repository.get_snapshot(run_id)
        visible = await app.state.session_repository.list_visible_messages(session)
        events = (await client.get(f"/api/tasks/{run_id}/events")).json()["events"]
        late_events = [event for event in events if event["event_type"] == "tool.late_result"]
        assert final["status"] == "cancelled"
        assert provider.calls == 2
        assert [message.content for message in visible] == ["Explain the project"]
        assert "late private contents" not in snapshot.model_dump_json()
        assert "late private contents" not in json.dumps([event["payload"] for event in events])
        assert len(late_events) == 1
        assert "result_digest" in late_events[0]["payload"]["detail"]["data"]
        assert broker.calls == ["read_file", "revoke_run"]


async def test_message_events_use_existing_http_cursor_and_versioned_payload(tmp_path):
    from app.assistant.models import RuntimeEvent

    responses = [decision("message", {"text": "first"}), decision("message", {"text": "second"}),
                 decision("finish", {"result": {"answer": "done"}})]
    async with assistant_api(tmp_path, responses) as (app, client, session, provider):
        response = await client.post(f"/api/sessions/{session}/tasks", json=request())
        run_id = response.json()["run_id"]
        assert await app.state.execution_worker.run_once()
        page = (await client.get(f"/api/tasks/{run_id}/events")).json()
        messages = [item for item in page["events"] if item["event_type"] == "assistant.message"]
        assert [item["payload"]["detail"]["data"]["text"] for item in messages] == ["first", "second"]
        for item in messages:
            parsed = RuntimeEvent.model_validate_json(json.dumps(item["payload"]))
            assert str(parsed.event_id) == item["id"]
            with pytest.raises(ValueError):
                RuntimeEvent.model_validate_json(json.dumps({**item["payload"], "schema_version": 2}))
        next_page = (await client.get(f"/api/tasks/{run_id}/events",
            params={"after": messages[0]["sequence"], "limit": 1})).json()
        assert next_page["events"][0]["sequence"] > messages[0]["sequence"]
        assert next_page["next_cursor"] == next_page["events"][0]["sequence"]
        assert provider.calls == 3


async def register(app, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "tests").mkdir()
    (root / "cache.py").write_text("CACHE_TTL = 60\n")
    return await app.state.workspace_service.register_original(root, ["tests"])


async def test_workspace_wait_bind_once_resume_and_budget(tmp_path):
    responses = [decision("call_tool", {"name": "read_file", "contract_version": "1", "arguments": {"path": "cache.py"}}),
                 decision("call_tool", {"name": "read_file", "contract_version": "1", "arguments": {"path": "cache.py"}}),
                 decision("finish", {"result": {"answer": "TTL is 60"}})]
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
        assert (await client.post(route, json=action)).status_code == 409
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
    responses = [decision("call_tool", {"name": "read_file", "contract_version": "1", "arguments": {"path": "cache.py"}})]
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
