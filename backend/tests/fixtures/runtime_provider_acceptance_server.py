"""Isolated opt-in server for Runtime v1 real-Provider acceptance."""

import asyncio
import contextvars
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.application import create_app
from app.assistant.strategy import ASSISTANT_DECISION_RESPONSE_FORMAT, AssistantStrategy
from app.knowledge.models import Chunk, Document
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.service import KnowledgeService
from app.settings import Settings
from app.llm.compatible_client import _REQUEST_CONTEXT, _REQUEST_CORRELATION
from app.observability.provider_attempt_recorder import ProviderAttemptRecorder
from tests.fixtures.provider_acceptance_runtime import CappedProvider
try:
    from tests.fixtures.provider_acceptance_executor import count_recorded_calls
    from tests.fixtures.provider_acceptance_replay import ProviderReplayClient
except ModuleNotFoundError:
    try:
        from fixtures.provider_acceptance_executor import count_recorded_calls
        from fixtures.provider_acceptance_replay import ProviderReplayClient
    except ModuleNotFoundError:
        from provider_acceptance_executor import count_recorded_calls
        from provider_acceptance_replay import ProviderReplayClient


backend = Path(__file__).parents[2]
root = Path(os.environ["RUNTIME_ACCEPTANCE_ROOT"]).resolve()
if not os.environ.get("RUNTIME_ACCEPTANCE_RESUME"):
    assert not (root / "agent.db").exists()
project = root / "project"
project.mkdir(parents=True, exist_ok=True)
cache_fixture = project / "cache.py"
test_fixture = project / "test_cache.py"
expected_cache = "def cache_record(redis, key, value):\n    return redis.setex(key, 60, value)\n"
expected_test = "# Read-only acceptance fixture; tests are never executed.\n"
if cache_fixture.exists() and cache_fixture.read_text(encoding="utf-8") != expected_cache:
    raise RuntimeError("existing acceptance project fixture differs; refusing to overwrite")
if test_fixture.exists() and test_fixture.read_text(encoding="utf-8") != expected_test:
    raise RuntimeError("existing acceptance test fixture differs; refusing to overwrite")
cache_fixture.write_text(expected_cache, encoding="utf-8")
test_fixture.write_text(expected_test, encoding="utf-8")
knowledge_path = root / "knowledge.db"
config = Settings(
    _env_file=backend / ".env" if os.environ.get("RUNTIME_ACCEPTANCE_MODE") != "replay" else None,
    database_path=root / "agent.db", docs_root=project,
    draft_directory=root / "drafts", code_task_workspace_root=root / "metadata",
    code_task_artifact_root=root / "artifacts", workspace_allowed_roots=[project],
    task_worker_enabled=True, assistant_enabled=True, code_task_sandbox_backend="local",
    writing_retrieval_mode="keyword", knowledge_pipeline_enabled=False,
    knowledge_query_planning_enabled=False, knowledge_rerank_enabled=False,
    embedding_api_key="", embedding_model="", embedding_dimensions=0,
    runtime_max_loops=6, runtime_max_tool_calls=5,
    runtime_max_duration_seconds=int(os.environ["RUNTIME_ACCEPTANCE_RUN_SECONDS"]),
    runtime_tool_timeout_seconds=20,
    llm_timeout_seconds=int(os.environ["RUNTIME_ACCEPTANCE_LLM_TIMEOUT_SECONDS"]),
)
if os.environ.get("RUNTIME_ACCEPTANCE_MODE") == "replay":
    config.llm_api_key = ""
else:
    assert config.llm_api_key.strip(), "real Provider must be configured in backend/.env"
app = create_app(config)
app.add_middleware(
    CORSMiddleware, allow_origins=[os.environ["RUNTIME_ACCEPTANCE_FRONTEND_ORIGIN"]],
    allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Content-Type"],
)
knowledge = KnowledgeRepository(knowledge_path)
knowledge_service = KnowledgeService(knowledge, pipeline=None)
deps = app.state.dependencies
if os.environ.get("RUNTIME_ACCEPTANCE_MODE") != "replay":
    deps.task_service.assistant.llm.response_format_identity = ASSISTANT_DECISION_RESPONSE_FORMAT
if os.environ.get("RUNTIME_ACCEPTANCE_MODE") == "replay":
    replay_path = Path(os.environ["RUNTIME_ACCEPTANCE_REPLAY_SOURCE"]).resolve()
    if not replay_path.is_relative_to(root.resolve()):
        raise RuntimeError("replay source must be copied into the isolated acceptance directory")
    replay_client = ProviderReplayClient(replay_path)
    deps.task_service.assistant.llm = replay_client
    deps.task_service.model_available = True
elif os.environ.get("RUNTIME_ACCEPTANCE_STREAM") == "1":
    from app.llm.compatible_client import OpenAICompatibleClient

    deps.task_service.assistant.llm = OpenAICompatibleClient(
        api_key=config.llm_api_key,
        base_url=config.llm_base_url,
        model=config.llm_model,
        timeout_seconds=config.llm_timeout_seconds,
        stream=True,
    )
deps.knowledge_repository = knowledge
deps.knowledge_service = knowledge_service
app.state.knowledge_repository = knowledge
app.state.knowledge_service = knowledge_service
for name in ("search_knowledge", "read_knowledge", "answer_knowledge"):
    deps.task_service.assistant.registry.get(name).service = knowledge_service
workspace_record = asyncio.run(deps.workspace_service.register_original(project, ["test_cache.py"]))

_strategy_decide = AssistantStrategy.decide


async def _recording_decide(self, state, available_tools, llm_response=None):
    initial_content = getattr(llm_response, "content", None)
    try:
        return await _strategy_decide(self, state, available_tools, llm_response)
    except ValueError as exc:
        current_content = getattr(llm_response, "content", None)
        if str(exc) == "invalid_assistant_protocol_json" and isinstance(initial_content, str):
            content = initial_content
            record = {
                "run_id": str(getattr(self, "run_id", "unknown")),
                "content": content,
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "content_changed_during_decide": current_content != initial_content,
            }
            cause = exc.__cause__
            if isinstance(cause, json.JSONDecodeError):
                record["json_error"] = {
                    "message": cause.msg,
                    "position": cause.pos,
                    "line": cause.lineno,
                    "column": cause.colno,
                }
            (root / "protocol-errors.jsonl").open("a", encoding="utf-8").write(
                json.dumps(record, ensure_ascii=False) + "\n"
            )
        raise


AssistantStrategy.decide = _recording_decide
deps.task_service.assistant.llm = CappedProvider(deps.task_service.assistant.llm)
_assistant_execute = deps.task_service.assistant.execute


async def _recording_execute(run, *args, **kwargs):
    token = request_run.set(str(run.id))
    try:
        return await _assistant_execute(run, *args, **kwargs)
    finally:
        request_run.reset(token)


deps.task_service.assistant.execute = _recording_execute
run_requests = set()
request_run = contextvars.ContextVar("acceptance_run_id", default=None)


@app.middleware("http")
async def restrict_acceptance(request, call_next):
    if request.method == "POST" and request.url.path.startswith("/api/sessions/") and request.url.path.endswith("/tasks"):
        body = await request.json()
        key = (request.url.path, body.get("client_request_id"))
        if body.get("kind") != "assistant" or key not in run_requests and len(run_requests) >= int(os.environ["RUNTIME_ACCEPTANCE_MAX_RUNS"]):
            return JSONResponse({"detail": {"code": "acceptance_scope_exhausted"}}, status_code=403)
        run_requests.add(key)
    elif request.method == "POST" and request.url.path == "/api/workspaces":
        return JSONResponse({"detail": {"code": "acceptance_workspace_pre_registered"}}, status_code=403)
    elif request.method == "POST" and request.url.path.startswith("/api/sessions/") and request.url.path.endswith("/workspace"):
        body = await request.json()
        if body.get("workspace_id") != workspace_record.workspace_id:
            return JSONResponse({"detail": {"code": "acceptance_workspace_restricted"}}, status_code=403)
    elif request.method == "POST" and request.url.path not in {"/api/sessions"} and not request.url.path.endswith("/actions") and not request.url.path.endswith("/workspace"):
        return JSONResponse({"detail": {"code": "acceptance_read_only"}}, status_code=403)
    return await call_next(request)


async def seed():
    await knowledge.init()
    await knowledge.replace_document(
        Document("doc-redis", "redis.md", None, "Redis SETEX", "v1"),
        [Chunk("chunk-setex", "doc-redis", "v1", "Redis > SETEX", 1, 3,
               "SETEX key seconds value atomically sets a Redis key value and expiration in seconds. "
               "When the TTL expires, Redis treats the key as absent.", 50)],
    )


asyncio.run(seed())
(root / "config-summary.json").write_text(json.dumps({
    "mode": os.environ.get("RUNTIME_ACCEPTANCE_MODE", "real"),
    "model": config.llm_model,
    "provider_host": urlsplit(config.llm_base_url).hostname,
    "max_runs": int(os.environ["RUNTIME_ACCEPTANCE_MAX_RUNS"]),
    "max_provider_calls": int(os.environ["RUNTIME_ACCEPTANCE_MAX_CALLS"]),
    "stream": os.environ.get("RUNTIME_ACCEPTANCE_STREAM") == "1",
    "max_run_seconds": int(os.environ["RUNTIME_ACCEPTANCE_RUN_SECONDS"]),
    "embedding": False,
    "sandbox": "not_used",
    "workspace_id": workspace_record.workspace_id,
    "workspace_path": str(project),
}, indent=2), encoding="utf-8")