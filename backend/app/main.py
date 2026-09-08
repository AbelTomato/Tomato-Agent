from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.agent.context import ContextManager
from app.agent.interfaces import LLMClient
from app.agent.runtime import AgentRuntime
from app.agent.models import Message, ToolDefinition, LLMResponse
from app.settings import settings
from app.sessions.repository import SessionRepository
from app.tools.calculator import Calculator
from app.tools.read_docs import ReadDocs
from app.tools.registry import ToolRegistry
from app.tools.search import Search
from app.llm.compatible_client import OpenAICompatibleClient

repo = SessionRepository(settings.database_path)
registry = ToolRegistry([Calculator(), Search(), ReadDocs(Path(settings.docs_root))])


class UnconfiguredLLM:
    async def complete(self, messages: list[Message], tools: list[ToolDefinition]) -> LLMResponse:
        raise RuntimeError("No LLMClient has been configured")


def create_llm_client() -> LLMClient:
    if not settings.llm_api_key:
        return UnconfiguredLLM()

    return OpenAICompatibleClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds
    )


llm_client: LLMClient = create_llm_client()


def create_runtime() -> AgentRuntime:
    return AgentRuntime(
        llm=llm_client,
        context_manager=ContextManager(),
        tool_registry=registry,
        repository=repo,
        system_instruction="You are a concise assistant.",
        config=settings.runtime_config,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await repo.init()
    yield


app = FastAPI(title="Tomato Agent Infrastructure", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


class CreateSessionRequest(BaseModel):
    metadata: dict = Field(default_factory=dict)


class RunRequest(BaseModel):
    message: str


@app.get("/health")
async def health():
    return {"status": "ok", "runtime": "runtime-implemented"}


@app.get("/api/tools")
async def tools():
    return {"tools": [item.model_dump() for item in registry.definitions()]}


@app.post("/api/sessions")
async def create_session(request: CreateSessionRequest):
    return {"session_id": str(await repo.create_session(request.metadata))}


@app.post("/api/sessions/{session_id}/runs")
async def create_run(session_id: str, request: RunRequest):
    try:
        ident = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(400, "Invalid session_id") from exc
    if not await repo.session_exists(ident):
        raise HTTPException(404, "Session not found")
    result = await create_runtime().run(ident, request.message)
    return result.model_dump(mode="json")
