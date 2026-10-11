"""Local acceptance server; owns only its explicitly selected isolated databases."""

import asyncio
import os
from pathlib import Path

from app.application import create_app
from app.knowledge.models import Chunk, Document
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.service import KnowledgeService
from app.settings import Settings
from app.writing.research import WritingResearcher
from fastapi.middleware.cors import CORSMiddleware


root = Path(os.environ["UNIFIED_ACCEPTANCE_ROOT"])
assert root.is_absolute() and root.is_relative_to(Path(__file__).parents[2] / "data" / "unified-mvp-acceptance")
project = root / "browser-project"
project.mkdir(parents=True, exist_ok=True)
if not (project / "calculator.py").exists():
    (project / "calculator.py").write_text("def add(a, b):\n    return a - b\n\ndef multiply(a, b):\n    return a + b\n")
    (project / "test_calculator.py").write_text("from calculator import add, multiply\ndef test_add():\n    assert add(2, 3) == 5\ndef test_multiply():\n    assert multiply(2, 2) == 4\n")
    (project / "notes.txt").write_text("pre-existing browser user modification\n")
config = Settings(database_path=root / "browser-agent.db", docs_root=project,
    draft_directory=root / "drafts", code_task_workspace_root=root / "metadata",
    code_task_artifact_root=root / "artifacts", workspace_allowed_roots=[project],
    task_worker_enabled=True, code_task_sandbox_backend="docker",
    code_task_docker_image=os.environ["SANDBOX_IMAGE"], writing_retrieval_mode="keyword",
    knowledge_pipeline_enabled=False, knowledge_rerank_enabled=False,
    embedding_model="", embedding_dimensions=0,
    code_task_max_duration_seconds=300, code_task_tool_timeout_seconds=60,
    code_task_max_loops=20, code_task_max_tool_calls=16)
assert config.llm_api_key.strip(), "real Provider must be configured"
app = create_app(config)
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5188"],
    allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Content-Type"])
knowledge_path = Path(os.environ["UNIFIED_ACCEPTANCE_KNOWLEDGE"])
assert knowledge_path.parent == Path(__file__).parents[2] / "data" / "rag" / "databases"
knowledge = KnowledgeRepository(knowledge_path)
deps = app.state.dependencies
deps.knowledge_repository = knowledge
deps.knowledge_service = KnowledgeService(knowledge, pipeline=None)
deps.task_service.adapters["knowledge"].conversation.service = deps.knowledge_service
deps.writing_executor.researcher = WritingResearcher(deps.knowledge_service)
app.state.knowledge_repository = knowledge
app.state.knowledge_service = deps.knowledge_service


async def seed():
    await knowledge.init()
    await knowledge.replace_document(Document("doc-redis", "redis.md", None, "Redis SETEX", "v1"), [
        Chunk("chunk-setex", "doc-redis", "v1", "Redis > SETEX", 1, 3,
              "SETEX key seconds value atomically sets a Redis key value and expiration in seconds. "
              "When the TTL expires, Redis treats the key as absent. SETEX avoids a separate SET then EXPIRE race.", 50)])


asyncio.run(seed())
