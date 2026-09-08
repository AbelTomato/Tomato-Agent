import app.main as main
import httpx
from app.agent.models import LLMResponse
from app.main import app, health
import pytest


def test_fastapi_app_is_importable():
    assert app.title == "Tomato Agent Infrastructure"


@pytest.mark.asyncio
async def test_health_response():
    assert await health() == {"status": "ok", "runtime": "runtime-implemented"}


@pytest.mark.asyncio
async def test_cors_preflight_allows_local_frontend_origin():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8000",
    ) as client:
        response = await client.options(
            "/api/sessions",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "POST" in response.headers["access-control-allow-methods"]


@pytest.mark.asyncio
async def test_cors_does_not_allow_unconfigured_origin():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8000",
    ) as client:
        response = await client.options(
            "/api/sessions",
            headers={
                "Origin": "http://malicious.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


class FakeLLM:
    async def complete(self, messages, tools):
        return LLMResponse(kind="final", content="api answer")


@pytest.mark.asyncio
async def test_create_run_uses_agent_runtime(monkeypatch, tmp_path):
    repository = main.SessionRepository(tmp_path / "api.db")
    monkeypatch.setattr(main, "repo", repository)
    await repository.init()
    monkeypatch.setattr(main, "llm_client", FakeLLM())

    session_id = await repository.create_session()
    result = await main.create_run(str(session_id), main.RunRequest(message="hello"))

    assert result["status"] == "completed"
    assert result["answer"] == "api answer"
