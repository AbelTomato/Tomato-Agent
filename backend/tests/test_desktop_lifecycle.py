import asyncio

import httpx
import pytest

from test_desktop_connection import desktop_app, headers


async def test_duplicate_attach_and_reconnect_are_idempotent(tmp_path):
    app, stopped = desktop_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers()) as client:
            for _ in range(2):
                assert (await client.post("/api/desktop/attach")).json()["connections"] == 1
            assert (await client.post("/api/desktop/attach", headers=headers("window-2"))).json()["connections"] == 2
            result = await client.post("/api/desktop/close", headers=headers(owner=True))
            assert result.json()["shutdown"] is False
            assert stopped == []
            assert (await client.post("/api/desktop/attach")).json()["connections"] == 2


@pytest.mark.parametrize("owned,owner_header,shutdown", [(True, True, True), (True, False, False), (False, True, False)])
async def test_only_idle_owned_service_can_shutdown(tmp_path, owned, owner_header, shutdown):
    app, stopped = desktop_app(tmp_path, owned=owned)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers(owner=owner_header)) as client:
            await client.post("/api/desktop/attach")
            response = await client.post("/api/desktop/close")
            assert response.json()["shutdown"] is shutdown
            assert bool(stopped) is shutdown
            if shutdown:
                assert (await client.post("/api/sessions", json={})).status_code == 503


@pytest.mark.parametrize("status", ["completed", "waiting", "running", "queued"])
async def test_disconnect_preserves_sqlite_run_and_reconnect_queries_same_run(tmp_path, status):
    app, stopped = desktop_app(tmp_path)
    async with app.router.lifespan_context(app):
        app.state.task_service.enabled = True
        app.state.task_service.model_available = True
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers(owner=True)) as client:
            await client.post("/api/desktop/attach")
            session = (await client.post("/api/sessions", json={})).json()["session_id"]
            created = await client.post(f"/api/sessions/{session}/tasks", json={
                "kind": "chat", "message": "hello", "client_request_id": "once"})
            assert created.status_code == 202
            run_id = created.json()["run_id"]
            repository = app.state.run_repository
            if status != "queued":
                await repository.update_status(run_id, "running")
                if status != "running":
                    await repository.update_status(run_id, status)
            before = (await client.get(f"/api/tasks/{run_id}")).json()
            response = await client.post("/api/desktop/close")
            assert response.json()["shutdown"] is (status == "completed")
            assert bool(stopped) is (status == "completed")
            assert (await repository.get_run(run_id)).status == status
            if status != "completed":
                await client.post("/api/desktop/attach")
                assert (await client.get(f"/api/tasks/{run_id}")).json() == before
                assert len(await repository.list_for_session(session)) == 1


async def test_owner_close_defers_shutdown_until_active_run_finishes(tmp_path):
    app, stopped = desktop_app(tmp_path)
    async with app.router.lifespan_context(app):
        app.state.task_service.enabled = True
        app.state.task_service.model_available = True
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     headers=headers(owner=True)) as client:
            await client.post("/api/desktop/attach")
            session = (await client.post("/api/sessions", json={})).json()["session_id"]
            created = await client.post(f"/api/sessions/{session}/tasks", json={
                "kind": "chat", "message": "hello", "client_request_id": "once"})
            assert created.status_code == 202
            run_id = created.json()["run_id"]
            await app.state.run_repository.update_status(run_id, "running")

            response = await client.post("/api/desktop/close")
            assert response.json()["shutdown"] is False
            assert response.json()["shutdown_pending"] is True
            assert stopped == []

            await app.state.run_repository.update_status(run_id, "completed")
            for _ in range(20):
                if stopped:
                    break
                await asyncio.sleep(0.1)
            assert stopped == [True]
