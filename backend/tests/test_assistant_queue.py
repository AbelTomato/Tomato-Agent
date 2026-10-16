import asyncio

from test_assistant_api import assistant_api, decision, register, request


async def enqueue(client, session, key, message):
    return await client.post(f"/api/sessions/{session}/queue", json=request(
        client_request_id=key, message=message))


async def test_queue_persists_deduplicates_and_starts_in_order_after_completion(tmp_path):
    responses = [decision("ask_user", {"question": "details?"}),
                 decision("finish", {"result": {"answer": "first"}}),
                 decision("finish", {"result": {"answer": "second"}}),
                 decision("finish", {"result": {"answer": "third"}})]
    async with assistant_api(tmp_path, responses) as (app, client, session, provider):
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        await app.state.execution_worker.run_once()
        answers = await asyncio.gather(enqueue(client, session, "second", "second"),
                                       enqueue(client, session, "second", "second"))
        assert all(item.status_code == 202 for item in answers)
        assert answers[0].json()["request_id"] == answers[1].json()["request_id"]
        assert answers[0].json()["run_id"] is None
        assert (await enqueue(client, session, "second", "different")).status_code == 409
        assert (await enqueue(client, session, "third", "third")).status_code == 202
        from app.runs.repository import RunRepository
        restored = RunRepository(app.state.run_repository.path)
        assert len((await restored.list_queued_requests(session))["requests"]) == 2
        assert not await app.state.execution_worker.run_once()
        waiting = (await client.get(f"/api/tasks/{run_id}")).json()
        await client.post(f"/api/tasks/{run_id}/actions", json={"action": "provide_input",
            "action_id": waiting["pending_action"]["action_id"], "expected_version": waiting["version"],
            "client_request_id": "answer", "payload": {"message": "details"}})
        assert await app.state.execution_worker.run_once()
        assert len(await app.state.run_repository.list_for_session(session)) == 1
        assert await app.state.execution_worker.run_once()
        assert await app.state.execution_worker.run_once()
        assert not await app.state.execution_worker.run_once()
        runs = await app.state.run_repository.list_for_session(session)
        assert [run.request["message"] for run in runs] == ["Explain the project", "second", "third"]
        assert provider.calls == 4
        queue = (await client.get(f"/api/sessions/{session}/queue")).json()
        assert [item["status"] for item in queue["requests"]] == ["started", "started"]


async def test_pending_queue_survives_application_restart_and_starts_once(tmp_path):
    responses = [decision("finish", {"result": {"answer": "started once"}})]
    async with assistant_api(tmp_path, responses) as (app, client, session, provider):
        accepted = await enqueue(client, session, "after-restart", "continue after restart")
        assert accepted.status_code == 202
        database_path = app.state.run_repository.path
        session_id = session
        assert (await app.state.run_repository.list_for_session(session)) == []

    async with assistant_api(tmp_path, responses) as (app, client, session, provider):
        queue = (await client.get(f"/api/sessions/{session_id}/queue")).json()
        assert [item["status"] for item in queue["requests"]] == ["pending"]
        assert await app.state.execution_worker.run_once()
        assert not await app.state.execution_worker.run_once()
        runs = await app.state.run_repository.list_for_session(session_id)
        assert len(runs) == 1
        assert runs[0].request["message"] == "continue after restart"
        assert provider.calls == 1
        queue = (await client.get(f"/api/sessions/{session_id}/queue")).json()
        assert queue["requests"][0]["status"] == "started"
        assert queue["requests"][0]["run_id"] == str(runs[0].id)
        assert str(database_path) == str(app.state.run_repository.path)


async def test_competing_dispatchers_create_only_one_run_for_pending_request(tmp_path):
    async with assistant_api(tmp_path, [decision("finish", {"result": {"answer": "once"}})]) as (app, client, session, provider):
        accepted = await enqueue(client, session, "concurrent-start", "start once")
        assert accepted.status_code == 202

        await asyncio.gather(app.state.task_service.dispatch_queue(), app.state.task_service.dispatch_queue())

        runs = await app.state.run_repository.list_for_session(session)
        queue = (await client.get(f"/api/sessions/{session}/queue")).json()
        assert len(runs) == 1
        assert queue["requests"][0]["status"] == "started"
        assert queue["requests"][0]["run_id"] == str(runs[0].id)
        assert await app.state.execution_worker.run_once()
        assert not await app.state.execution_worker.run_once()
        assert provider.calls == 1


async def test_cancel_pauses_queue_until_explicit_continue_and_clear_is_independent(tmp_path):
    async with assistant_api(tmp_path, [decision("finish", {"result": {"answer": "next"}})]) as (app, client, session, _):
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        queued = await enqueue(client, session, "next", "next")
        assert queued.status_code == 202
        current = (await client.get(f"/api/tasks/{run_id}")).json()
        await client.post(f"/api/tasks/{run_id}/actions", json={"action": "cancel", "action_id": "cancel",
            "expected_version": current["version"], "client_request_id": "cancel", "payload": {}})
        assert not await app.state.execution_worker.run_once()
        paused = (await client.get(f"/api/sessions/{session}/queue")).json()
        assert paused["paused"] is True
        assert paused["requests"][0]["status"] == "pending"
        resumed = await client.post(f"/api/sessions/{session}/queue/resume")
        assert resumed.status_code == 200 and resumed.json()["paused"] is False
        assert await app.state.execution_worker.run_once()
        assert (await client.get(f"/api/sessions/{session}/queue")).json()["requests"][0]["status"] == "started"
        assert (await enqueue(client, session, "clear", "clear")).status_code == 202
        cleared = await client.post(f"/api/sessions/{session}/queue/clear")
        assert cleared.status_code == 200
        assert cleared.json()["requests"][-1]["status"] == "cancelled"
        assert not await app.state.execution_worker.run_once()


async def test_steer_is_recorded_then_applied_at_safe_point_without_resetting_budget(tmp_path):
    responses = [decision("message", {"text": "working"}),
                 decision("finish", {"result": {"answer": "revised"}})]
    async with assistant_api(tmp_path, responses) as (app, client, session, provider):
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        started, release = asyncio.Event(), asyncio.Event()
        original = provider.complete
        async def hold(messages, tools, *, response_format=None):
            if provider.calls == 0:
                started.set()
                await release.wait()
            return await original(messages, tools, response_format=response_format)
        provider.complete = hold
        execution = asyncio.create_task(app.state.execution_worker.run_once())
        await started.wait()
        current = (await client.get(f"/api/tasks/{run_id}")).json()
        response = await client.post(f"/api/tasks/{run_id}/steer", json={"client_request_id": "steer-1",
            "message": "change the requested direction"})
        if response.status_code != 202:
            release.set()
            await execution
        assert response.status_code == 202 and response.json()["status"] == "requested"
        assert (await client.post(f"/api/tasks/{run_id}/steer", json={"client_request_id": "steer-1",
            "message": "change the requested direction"})).json()["status"] == "requested"
        assert (await client.get(f"/api/tasks/{run_id}")).json()["version"] == current["version"]
        release.set()
        assert await execution
        completed = (await client.get(f"/api/tasks/{run_id}")).json()
        assert completed["status"] == "completed"
        assert [event["event_type"] for event in (await client.get(f"/api/tasks/{run_id}/events")).json()["events"]].count("assistant.steer_applied") == 1
        assert provider.calls == 2
        checkpoint = await app.state.execution_repository.get_snapshot(run_id)
        assert any("change the requested direction" in item.content for item in checkpoint.messages)
        assert checkpoint.reserved_model_calls == 2


async def test_steer_during_preflight_abandons_old_tool_before_start(tmp_path):
    responses = [decision("call_tool", {"name": "read_file", "contract_version": "1",
        "arguments": {"path": "cache.py"}}), decision("finish", {"result": {"answer": "changed"}})]
    async with assistant_api(tmp_path, responses) as (app, client, session, provider):
        workspace = await register(app, tmp_path)
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request(
            workspace_id=workspace.workspace_id))).json()["run_id"]
        tool = app.state.task_service.assistant.registry.get("read_file")
        started, release = asyncio.Event(), asyncio.Event()
        async def preflight(arguments, context):
            started.set()
            await release.wait()
        tool.preflight = preflight
        execution = asyncio.create_task(app.state.execution_worker.run_once())
        await asyncio.wait_for(started.wait(), 3)
        response = await client.post(f"/api/tasks/{run_id}/steer", json={
            "client_request_id": "change", "message": "skip reading files"})
        release.set()
        assert response.status_code == 202
        assert await execution
        events = (await client.get(f"/api/tasks/{run_id}/events")).json()["events"]
        assert "tool.started" not in [event["event_type"] for event in events]
        assert "tool.abandoned" in [event["event_type"] for event in events]
        assert (await client.get(f"/api/tasks/{run_id}")).json()["status"] == "completed"
        assert provider.calls == 2