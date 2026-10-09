import httpx
import pytest

from app.agent.models import LLMResponse
from app.application import create_app
from app.settings import Settings
from app.writing.citation_models import WritingCitation


@pytest.mark.asyncio
async def test_create_app_uses_supplied_settings_and_registers_routers(tmp_path):
    config = Settings(
        database_path=tmp_path / "app.db",
        docs_root=tmp_path,
        code_task_tool_timeout_seconds=3.5,
        code_task_max_tool_result_chars=123,
        code_task_max_loops=2,
        code_task_max_tool_calls=4,
        code_task_max_context_tokens=456,
        code_task_max_response_chars=789,
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
    assert app.state.dependencies.run_repository.path == config.database_path
    workspace = await app.state.dependencies.workspace_service.create()
    run = type("Run", (), {"workspace_id": workspace.workspace_id})()
    profile = app.state.dependencies.code_task_service.build_capability_profile(run)
    assert profile.timeout_seconds == 3.5
    assert profile.max_output_chars == 123
    budget = app.state.dependencies.code_task_service.budget
    assert budget.max_loops == 2
    assert budget.max_tool_calls == 4
    assert budget.max_context_tokens == 456
    assert budget.max_response_chars == 789
    assert app.state.dependencies.knowledge_service.pipeline is None
    assert app.state.dependencies.writing_research_service.pipeline is None
    assert "/api/tools" in paths


@pytest.mark.asyncio
async def test_create_app_initializes_run_repository(tmp_path):
    config = Settings(database_path=tmp_path / "app.db", docs_root=tmp_path)
    app = create_app(config)

    async with app.router.lifespan_context(app):
        run = await app.state.run_repository.create_run(
            "code_task", {"task": "verify"}, "workspace-1"
        )

    assert run.status == "queued"


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
