"""Opt-in isolated real-Provider server for the task-5 read-only acceptance."""

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.application import create_app
from app.knowledge.models import Chunk, Document
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.service import KnowledgeService
from app.settings import Settings


backend = Path(__file__).parents[2]
root = Path(os.environ["ASSISTANT_ACCEPTANCE_ROOT"])
assert root.is_absolute() and root.is_relative_to(backend / "data" / "assistant-kernel-acceptance")
assert not (root / "agent.db").exists(), "use a fresh acceptance directory"
project = root / "project"
project.mkdir(parents=True, exist_ok=False)
(project / "cache.py").write_text(
    'def cache_record(redis, key, value):\n    return redis.setex(key, 60, value)\n', encoding="utf-8")
(project / "test_cache.py").write_text('# Read-only acceptance: no tests are executed.\n', encoding="utf-8")
knowledge_path = Path(os.environ["ASSISTANT_ACCEPTANCE_KNOWLEDGE"])
assert knowledge_path.parent == backend / "data" / "rag" / "databases"
assert not knowledge_path.exists(), "use a fresh knowledge snapshot"
config = Settings(database_path=root / "agent.db", docs_root=project,
    draft_directory=root / "drafts", code_task_workspace_root=root / "metadata",
    code_task_artifact_root=root / "artifacts", workspace_allowed_roots=[project],
    task_worker_enabled=True, assistant_enabled=True, code_task_sandbox_backend="local",
    writing_retrieval_mode="keyword", knowledge_pipeline_enabled=False,
    knowledge_query_planning_enabled=False, knowledge_rerank_enabled=False,
    embedding_api_key="", embedding_model="", embedding_dimensions=0,
    runtime_max_loops=6, runtime_max_tool_calls=5, runtime_max_duration_seconds=120,
    runtime_tool_timeout_seconds=20, llm_timeout_seconds=45)
assert config.llm_api_key.strip(), "real Provider must be configured"
app = create_app(config)
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5190"],
    allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Content-Type"])
knowledge = KnowledgeRepository(knowledge_path)
service = KnowledgeService(knowledge, pipeline=None)
deps = app.state.dependencies
deps.knowledge_repository = knowledge
deps.knowledge_service = service
app.state.knowledge_repository = knowledge
app.state.knowledge_service = service
for name in ("search_knowledge", "read_knowledge", "answer_knowledge"):
    deps.task_service.assistant.registry.get(name).service = service


class CappedProvider:
    def __init__(self, client):
        self.client = client
        self.calls = 0
        self.timeout = getattr(client, "timeout", None)

    async def complete(self, messages, tools):
        if self.calls >= 12:
            raise RuntimeError("acceptance_total_model_budget_exhausted")
        self.calls += 1
        record = {"attempt": self.calls}
        try:
            response = await self.client.complete(messages, tools)
            record["response"] = response.model_dump(mode="json")
            return response
        except BaseException as exc:
            record["error_type"] = type(exc).__name__
            raise
        finally:
            with (root / "provider-attempts.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")


deps.task_service.assistant.llm = CappedProvider(deps.task_service.assistant.llm)
run_requests = set()


@app.middleware("http")
async def restrict_acceptance(request, call_next):
    if request.method == "POST" and request.url.path.startswith("/api/sessions/") and request.url.path.endswith("/tasks"):
        body = await request.json()
        key = (request.url.path, body.get("client_request_id"))
        if body.get("kind") != "assistant" or key not in run_requests and len(run_requests) >= 3:
            return JSONResponse({"detail": {"code": "acceptance_scope_exhausted"}}, status_code=403)
        run_requests.add(key)
    elif request.method == "POST" and not (
        request.url.path == "/api/sessions" or request.url.path == "/api/workspaces"
        or request.url.path.endswith("/actions") or request.url.path.endswith("/workspace")
    ):
        return JSONResponse({"detail": {"code": "acceptance_read_only"}}, status_code=403)
    return await call_next(request)


async def seed():
    await knowledge.init()
    await knowledge.replace_document(Document("doc-redis", "redis.md", None, "Redis SETEX", "v1"), [
        Chunk("chunk-setex", "doc-redis", "v1", "Redis > SETEX", 1, 3,
              "SETEX key seconds value atomically sets a Redis key value and expiration in seconds. "
              "When the TTL expires, Redis treats the key as absent. SETEX avoids a separate SET then EXPIRE race.", 50)])


asyncio.run(seed())
(root / "config-summary.json").write_text(json.dumps({
    "model": config.llm_model, "provider_host": urlsplit(config.llm_base_url).hostname,
    "max_runs": 3, "max_provider_calls": 12, "max_run_seconds": 120,
    "retrieval_mode": "keyword", "embedding": False, "sandbox": "not_used",
    "knowledge_database": str(knowledge_path), "project": str(project),
}, ensure_ascii=False, indent=2), encoding="utf-8")