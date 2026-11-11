import httpx
import pytest

from test_application_lifecycle import make_app


def desktop_app(tmp_path, *, owned=True):
    from app.desktop.connection import install_desktop

    app = make_app(tmp_path)
    stopped = []
    install_desktop(app, token="a" * 64, instance="b" * 32,
                    owner="owner" if owned else None, stop=lambda: stopped.append(True))
    return app, stopped


def headers(client="window-1", *, owner=False):
    value = {"Authorization": "Bearer " + "a" * 64, "X-Tomato-Instance": "b" * 32,
             "X-Tomato-Client": client}
    if owner:
        value["X-Tomato-Owner"] = "owner"
    return value


@pytest.mark.parametrize("path", ["/health", "/api/sessions", "/api/tasks/unknown",
                                  "/api/workspaces", "/api/tools",
                                  "/api/desktop/connection", "/api/memory/status"])
async def test_all_desktop_routes_reject_missing_credentials(tmp_path, path):
    app, _ = desktop_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(path)).status_code == 401
        assert (await client.post(path, json={})).status_code == 401


async def test_identity_and_credentials_are_checked_before_business_routes(tmp_path):
    app, _ = desktop_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/health", headers={**headers(), "Authorization": "Bearer wrong"})).status_code == 401
            assert (await client.get("/health", headers={**headers(), "X-Tomato-Instance": "other"})).status_code == 409
            result = await client.get("/api/desktop/connection", headers=headers())
            assert result.status_code == 200
            assert result.json()["service_instance_id"] == "b" * 32
            memory = await client.get("/api/memory/status", headers=headers())
            assert memory.status_code == 200
            assert memory.json() == {
                "protocol_version": 1,
                "enabled": False,
                "ready": True,
                "state": "ready",
                "generation": 0,
            }
            assert "a" * 64 not in result.text
            assert "owner" not in result.text


async def test_internal_engine_does_not_expose_desktop_attach_outside_desktop_mode(tmp_path):
    app = make_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/api/desktop/connection")).status_code == 404
            assert (await client.get("/docs")).status_code == 404
            assert (await client.get("/openapi.json")).status_code == 404
