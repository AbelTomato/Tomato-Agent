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
    assert "/api/tools" in paths