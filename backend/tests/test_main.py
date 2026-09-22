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
async def test_default_tool_registry_exposes_local_knowledge_tools():
    payload = await main.tools()
    names = {tool["name"] for tool in payload["tools"]}

    assert {"search_knowledge", "read_knowledge"}.issubset(names)


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


class RecordingLLM:
    def __init__(self):
        self.messages = []
        self.responses = [
            LLMResponse(kind="final", content="第一轮回答"),
            LLMResponse(kind="final", content="第二轮回答"),
        ]

    async def complete(self, messages, tools):
        self.messages.append(messages)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_http_runs_pass_completed_history_to_second_request(monkeypatch, tmp_path):
    repository = main.SessionRepository(tmp_path / "api.db")
    await repository.init()
    llm = RecordingLLM()
    monkeypatch.setattr(main, "repo", repository)
    monkeypatch.setattr(main, "llm_client", llm)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        session_response = await client.post("/api/sessions", json={})
        session_response.raise_for_status()
        session_id = session_response.json()["session_id"]

        first_response = await client.post(
            f"/api/sessions/{session_id}/runs",
            json={"message": "我的主题是 Redis"},
        )
        second_response = await client.post(
            f"/api/sessions/{session_id}/runs",
            json={"message": "继续讨论"},
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    second_contents = [message.content for message in llm.messages[1]]
    assert "我的主题是 Redis" in second_contents
    assert "第一轮回答" in second_contents
    assert second_contents.count("继续讨论") == 1

def test_knowledge_factory_keeps_vector_modes_disabled_without_embedding_config(tmp_path):
    config = main.Settings(embedding_model="", embedding_dimensions=0)

    service = main.create_knowledge_service(
        main.KnowledgeRepository(tmp_path / "knowledge.db"),
        config,
    )

    assert service.query_embedder is None
    assert service.embedding_model == ""
    assert service.embedding_dimensions == 0


def test_knowledge_factory_wires_configured_embedding_client(tmp_path):
    config = main.Settings(
        embedding_api_key="embedding-key",
        embedding_base_url="https://embedding.example/v1",
        embedding_model="test-embedding",
        embedding_dimensions=3,
    )

    service = main.create_knowledge_service(
        main.KnowledgeRepository(tmp_path / "knowledge.db"),
        config,
    )

    assert service.query_embedder is not None
    assert service.embedding_model == "test-embedding"
    assert service.embedding_dimensions == 3
    assert service.query_embedder.__self__.api_key == "embedding-key"
    assert service.query_embedder.__self__.base_url == "https://embedding.example/v1"


def test_knowledge_factory_keeps_pipeline_disabled_by_default(tmp_path):
    service = main.create_knowledge_service(
        main.KnowledgeRepository(tmp_path / "knowledge.db"),
        main.Settings(),
    )

    assert service.pipeline is None


def test_knowledge_factory_injects_explicit_pipeline_components(tmp_path):
    config = main.Settings(
        knowledge_pipeline_enabled=True,
        knowledge_candidate_limit=17,
    )

    service = main.create_knowledge_service(
        main.KnowledgeRepository(tmp_path / "knowledge.db"),
        config,
    )

    assert service.pipeline is not None
    assert service.candidate_limit == 17
    assert isinstance(service.pipeline.reranker, main.NoopReranker)


@pytest.mark.asyncio
async def test_knowledge_capabilities_only_expose_configured_modes(monkeypatch):
    unconfigured = main.KnowledgeService(object())
    monkeypatch.setattr(main, "knowledge_service", unconfigured)

    assert await main.knowledge_capabilities() == {
        "retrieval_modes": ["keyword"],
        "embedding_configured": False,
        "embedding_model": None,
        "embedding_dimensions": None,
    }

    configured = main.KnowledgeService(
        object(),
        query_embedder=lambda texts: texts,
        embedding_model="test-embedding",
        embedding_dimensions=3,
    )
    monkeypatch.setattr(main, "knowledge_service", configured)

    assert await main.knowledge_capabilities() == {
        "retrieval_modes": ["keyword", "vector", "hybrid"],
        "embedding_configured": True,
        "embedding_model": "test-embedding",
        "embedding_dimensions": 3,
    }

