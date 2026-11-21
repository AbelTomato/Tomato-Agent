from datetime import datetime, timezone
from uuid import UUID

import httpx

from test_desktop_connection import desktop_app, headers


async def test_trace_api_returns_cursor_page_only_to_attached_desktop_client(tmp_path):
    app, _ = desktop_app(tmp_path)
    app.state.trace_buffer.append(
        trace_id=UUID("00000000-0000-0000-0000-000000000001"),
        span_id=UUID("00000000-0000-0000-0000-000000000002"),
        parent_span_id=None,
        run_id=UUID("00000000-0000-0000-0000-000000000003"),
        operation="run.execute",
        started_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
        duration_ms=2.0,
        status="ok",
        error_code=None,
        error_category=None,
        attributes={},
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            path = "/api/desktop/traces"
            assert (await client.get(path, headers=headers())).status_code == 403
            await client.post("/api/desktop/attach", headers=headers())

            response = await client.get(path, headers=headers(), params={"after": 0, "limit": 500})

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "runtime-trace/v1"
    assert len(body["items"]) == 1
    assert body["items"][0]["sequence"] == 1
    assert body["next_cursor"] == 1
    assert body["runtime_instance_id"] == str(app.state.trace_buffer.runtime_instance_id)


async def test_trace_api_rejects_unattached_clients_and_invalid_page_sizes(tmp_path):
    app, _ = desktop_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            path = "/api/desktop/traces"
            assert (await client.get(path)).status_code == 401
            assert (await client.get(path, headers=headers("unknown"))).status_code == 403
            await client.post("/api/desktop/attach", headers=headers())
            assert (await client.get(path, headers=headers(), params={"limit": 501})).status_code == 422


async def test_internal_engine_does_not_expose_trace_api(tmp_path):
    from test_application_lifecycle import make_app

    app = make_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/desktop/traces")).status_code == 404
