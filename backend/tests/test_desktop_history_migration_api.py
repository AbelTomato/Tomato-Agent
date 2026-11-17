import httpx
import pytest

from test_application_lifecycle import make_app


@pytest.mark.asyncio
async def test_desktop_history_import_is_desktop_only_idempotent_and_visible(tmp_path):
    app = make_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            payload = {
                "installation_id": "installation-1",
                "source_digest": "fca8c67f07208a4a4cf3149faf8cf7a23b3279257d5b46ef5aad3eea8195086c",
                "sessions": [{
                    "id": "00000000-0000-0000-0000-000000000201",
                    "title": "历史会话",
                    "project_id": "project-1",
                    "created_at": "2026-11-01T00:00:00+00:00",
                }],
                "messages": [{
                    "id": "message-1",
                    "session_id": "00000000-0000-0000-0000-000000000201",
                    "sequence": 1,
                    "role": "user",
                    "content": "历史内容",
                    "created_at": "2026-11-01T00:00:01+00:00",
                }],
            }
            path = "/api/desktop/history/import"
            assert (await client.post(path, json=payload)).status_code == 404

            app.state.desktop_connection_installed = True
            first = await client.post(path, json=payload)
            replay = await client.post(path, json=payload)
            assert first.status_code == replay.status_code == 200
            assert first.json() == replay.json()
            status = await client.get(f"/api/desktop/history/import/{payload['installation_id']}")
            assert status.json() == first.json()
            session_id = payload["sessions"][0]["id"]
            messages = await client.get(f"/api/sessions/{session_id}/messages")
            assert messages.status_code == 200
            assert messages.json()["messages"][0]["id"] == "message-1"
            assert messages.json()["messages"][0]["content"] == "历史内容"
            sessions = await client.get("/api/sessions")
            assert sessions.json()["sessions"][0]["id"] == session_id
            assert sessions.json()["sessions"][0]["project_id"] == "project-1"
            assert sessions.json()["sessions"][0]["title"] == "历史会话"
            renamed = await client.patch(f"/api/sessions/{session_id}/title", json={
                "title": "重命名后的会话", "expected_version": 0,
            })
            assert renamed.status_code == 200
            assert renamed.json()["title"] == "重命名后的会话"
            stale = await client.patch(f"/api/sessions/{session_id}/title", json={
                "title": "过期更新", "expected_version": 0,
            })
            assert stale.status_code == 409
            moved = await client.patch(f"/api/sessions/{session_id}/project", json={"project_id": "project-next"})
            assert moved.status_code == 200 and moved.json()["project_id"] == "project-next"
            assert (await client.delete(f"/api/sessions/{session_id}")).status_code == 200
            assert (await client.get("/api/sessions")).json()["sessions"] == []
            assert (await client.get(f"/api/sessions/{session_id}/messages")).json()["messages"][0]["id"] == "message-1"


@pytest.mark.asyncio
async def test_desktop_history_import_rejects_extra_fields_and_changed_installation_replay(tmp_path):
    app = make_app(tmp_path)
    async with app.router.lifespan_context(app):
        app.state.desktop_connection_installed = True
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            payload = {
                "installation_id": "installation-2",
                "sessions": [{
                    "id": "00000000-0000-0000-0000-000000000202",
                    "title": "历史会话",
                    "project_id": None,
                    "created_at": "2026-11-01T00:00:00+00:00",
                }],
                "messages": [],
            }
            path = "/api/desktop/history/import"
            assert (await client.post(path, json={**payload, "unexpected": True})).status_code == 422
            mismatch = await client.post(path, json={**payload, "source_digest": "0" * 64})
            assert mismatch.status_code == 409
            assert mismatch.json()["detail"]["code"] == "desktop_history_digest_mismatch"
            assert (await client.post(path, json=payload)).status_code == 200
            changed = {**payload, "sessions": [{**payload["sessions"][0], "title": "changed"}]}
            conflict = await client.post(path, json=changed)
            assert conflict.status_code == 409
            assert conflict.json()["detail"]["code"] == "desktop_history_conflict"


@pytest.mark.asyncio
async def test_desktop_history_id_collision_rolls_back_whole_import(tmp_path):
    app = make_app(tmp_path)
    async with app.router.lifespan_context(app):
        app.state.desktop_connection_installed = True
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/api/sessions", json={"metadata": {"title": "existing"}})
            existing = (await client.get("/api/sessions")).json()["sessions"][0]["id"]
            payload = {
                "installation_id": "collision-installation",
                "sessions": [{"id": existing, "title": "imported", "project_id": None,
                              "created_at": "2026-11-01T00:00:00+00:00"}],
                "messages": [{"id": "collision-message", "session_id": existing, "sequence": 1,
                              "role": "user", "content": "must roll back",
                              "created_at": "2026-11-01T00:00:01+00:00"}],
            }
            response = await client.post("/api/desktop/history/import", json=payload)
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "desktop_history_conflict"
            assert (await client.get(f"/api/sessions/{existing}/messages")).json()["messages"] == []
            assert (await client.get(f"/api/desktop/history/import/{payload['installation_id']}")).status_code == 404
