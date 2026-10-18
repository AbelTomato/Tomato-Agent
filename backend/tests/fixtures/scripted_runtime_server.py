"""Offline browser fixture. Run with uvicorn --factory and an isolated root."""

import os
from pathlib import Path

from app.application import create_app
from app.settings import Settings
from tests.fixtures.scripted_runtime import ScriptedRuntimeClient


def create_scripted_app():
    backend = Path(__file__).resolve().parents[2]
    root = Path(os.environ["SCRIPTED_RUNTIME_ROOT"]).resolve()
    allowed = backend / "data" / "runtime-scripted-acceptance"
    if not root.is_relative_to(allowed) or root == allowed:
        raise ValueError("scripted Runtime requires its own isolated acceptance directory")
    if (root / "agent.db").exists():
        raise ValueError("use a fresh scripted Runtime directory")
    root.mkdir(parents=True, exist_ok=True)
    project = root / "project"
    project.mkdir(exist_ok=False)
    (project / "cache.py").write_text("CACHE_TTL = 60\n", encoding="utf-8")
    (project / "tests").mkdir()
    config = Settings(
        _env_file=None, llm_api_key="", embedding_api_key="", embedding_model="",
        database_path=root / "agent.db", docs_root=project,
        draft_directory=root / "drafts", code_task_workspace_root=root / "metadata",
        code_task_artifact_root=root / "artifacts", workspace_allowed_roots=[project],
        assistant_enabled=True, task_worker_enabled=True,
        knowledge_pipeline_enabled=False, knowledge_rerank_enabled=False,
        runtime_max_loops=8, runtime_max_tool_calls=2, runtime_max_duration_seconds=60,
    )
    app = create_app(config)
    app.state.task_service.assistant.llm = ScriptedRuntimeClient()
    app.state.task_service.model_available = True
    return app