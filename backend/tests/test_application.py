from app.application import create_app
from app.settings import Settings


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