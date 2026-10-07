import httpx
import pytest

from app.agent.models import LLMResponse
from app.application import create_app
from app.settings import Settings
from app.writing.citation_models import WritingCitation


def test_create_app_uses_supplied_settings_and_registers_routers(tmp_path):
    config = Settings(
        database_path=tmp_path / "app.db",
        docs_root=tmp_path,
        knowledge_pipeline_enabled=False,
        knowledge_rerank_enabled=False,
    )

    app = create_app(config)
    paths = set(app.openapi()["paths"])

    assert "/health" in paths
    assert "/api/sessions" in paths
    assert "/api/knowledge/capabilities" in paths
    assert "/api/sessions/{session_id}/writing-tasks" in paths
    assert app.state.dependencies.session_repository.path == config.database_path
    assert app.state.dependencies.knowledge_service.pipeline is None
    assert app.state.dependencies.writing_research_service.pipeline is None
    assert "/api/tools" in paths


def test_create_app_keeps_writing_research_deterministic_when_online_pipeline_enabled(tmp_path):
    config = Settings(
        database_path=tmp_path / "app.db",
        docs_root=tmp_path,
        embedding_api_key="embedding-key",
        embedding_model="test-embedding",
        embedding_dimensions=3,
        knowledge_pipeline_enabled=True,
        knowledge_rerank_enabled=False,
    )

    app = create_app(config)

    assert app.state.dependencies.knowledge_service.pipeline is not None
    assert app.state.dependencies.writing_research_service.pipeline is None
    assert app.state.dependencies.knowledge_service.embedding_model == "test-embedding"
    assert app.state.dependencies.writing_research_service.embedding_model == "test-embedding"


@pytest.mark.asyncio
async def test_create_app_generates_draft_with_configured_llm(tmp_path, monkeypatch):
    class FakeDraftLLM:
        def __init__(self):
            self.calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            assert tools == []
            return LLMResponse(
                kind="final",
                content=(
                    '{"title":"Redis","sections":[{"title":"过期",'
                    '"content":"SETEX 设置过期时间。","citation_ids":["chunk-1"]}]}'
                ),
            )

    llm = FakeDraftLLM()
    monkeypatch.setattr("app.dependencies.create_llm_client", lambda config: llm)
    app = create_app(Settings(
        _env_file=None,
        database_path=tmp_path / "app.db",
        docs_root=tmp_path,
        draft_directory=tmp_path / "drafts",
        llm_api_key="test-key",
        llm_model="test-model",
        embedding_api_key="",
        knowledge_pipeline_enabled=False,
        knowledge_rerank_enabled=False,
    ))

    async with app.router.lifespan_context(app):
        dependencies = app.state.dependencies
        session_id = await dependencies.session_repository.create_session()
        service = dependencies.writing_service
        task = await service.create_task(session_id, "Redis")
        task = await service.publish_outline(
            task.task_id,
            {
                "title": "Redis",
                "sections": [{
                    "title": "过期",
                    "points": ["说明 SETEX"],
                    "citation_ids": ["chunk-1"],
                }],
                "gaps": [],
            },
            [WritingCitation(
                citation_id="chunk-1", chunk_id="chunk-1", document_id="doc-1",
                document_version="v1", source_path="redis.md", source_url=None,
                title="Redis", heading_path="过期", start_line=1, end_line=1,
                text="SETEX 设置过期时间。",
            )],
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            unconfirmed = await client.post(
                f"/api/writing-tasks/{task.task_id}/draft",
                json={"version": task.version},
            )
            assert unconfirmed.status_code == 409
            assert llm.calls == 0

            confirmed = await client.post(
                f"/api/writing-tasks/{task.task_id}/confirm-outline",
                json={"version": task.version, "outline": task.outline},
            )
            assert confirmed.status_code == 200
            version = confirmed.json()["version"]
            response = await client.post(
                f"/api/writing-tasks/{task.task_id}/draft", json={"version": version},
            )
            assert response.status_code == 200, response.text
            generated = response.json()
            assert generated["status"] == "awaiting_save_confirmation"
            assert "SETEX 设置过期时间。" in generated["draft"]
            assert generated["version"] == version + 1
            assert llm.calls == 1

            repeated = await client.post(
                f"/api/writing-tasks/{task.task_id}/draft", json={"version": version},
            )
            assert repeated.status_code == 200
            assert repeated.json() == generated
            assert llm.calls == 1
            assert not (tmp_path / "drafts").exists()
