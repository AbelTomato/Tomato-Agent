"""Compatibility entrypoint for ``uvicorn app.main:app`` and legacy imports."""

from types import SimpleNamespace

from app.agent.context import ContextManager
from app.agent.interfaces import LLMClient
from app.agent.runtime import AgentRuntime
from app.application import create_app
from app.dependencies import (
    create_embedding_client,
    create_knowledge_service,
    create_llm_client,
    create_runtime,
)
from app.knowledge.embeddings import EmbeddingClient
from app.knowledge.pipeline_factory import runtime_config_from_settings
from app.knowledge.reranking import NoopReranker
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.service import CitationSnapshot, KnowledgeAnswer, KnowledgeService
from app.sessions.repository import SessionRepository
from app.settings import Settings, settings
from app.tools.registry import ToolRegistry
from app.writing.execution_models import ExecutionConfig
from app.writing.execution_repository import WritingExecutionRepository
from app.writing.executor import WritingTaskExecutor
from app.writing.repository import WritingRepository
from app.writing.service import WritingService
from fastapi import FastAPI
from app.api.knowledge import (
    KnowledgeRunRequest,
    create_knowledge_run as knowledge_run_route,
    get_knowledge_document as knowledge_document_route,
    knowledge_capabilities as knowledge_capabilities_route,
)
from app.api.sessions import (
    CreateSessionRequest,
    RunRequest,
    create_run as create_run_route,
    create_session as create_session_route,
    list_messages as list_messages_route,
)
from app.api.writing import router as writing_router


app = create_app()

# Temporary compatibility exports for callers/tests that used the former
# module-level objects. Application routes read dependencies from app.state.
repo = app.state.session_repository
knowledge_repository = app.state.knowledge_repository
writing_repository = app.state.writing_repository
writing_execution_repository = app.state.writing_execution_repository
registry = app.state.tool_registry
llm_client: LLMClient = app.state.llm_client
knowledge_service = app.state.knowledge_service
writing_service = app.state.writing_service
writing_executor = app.state.writing_executor


async def tools():
    return {"tools": [item.model_dump() for item in registry.definitions()]}


async def health():
    return {"status": "ok", "runtime": "runtime-implemented"}


async def create_session(request: CreateSessionRequest):
    session_id = await repo.create_session(request.metadata)
    return {"session_id": str(session_id)}


async def create_run(session_id: str, request: RunRequest):
    runtime = AgentRuntime(
        llm=llm_client,
        context_manager=ContextManager(),
        tool_registry=registry,
        repository=repo,
        system_instruction="You are a concise assistant.",
        config=settings.runtime_config,
    )
    state = SimpleNamespace(runtime=runtime)
    return await create_run_route(
        session_id,
        request,
        SimpleNamespace(app=SimpleNamespace(state=state)),
    )


async def knowledge_capabilities():
    state = SimpleNamespace(knowledge_service=knowledge_service)
    return await knowledge_capabilities_route(
        SimpleNamespace(app=SimpleNamespace(state=state))
    )


async def create_knowledge_run(session_id: str, request: KnowledgeRunRequest):
    dependencies = SimpleNamespace(
        session_repository=repo,
        knowledge_service=knowledge_service,
        llm_client=llm_client,
    )
    state = SimpleNamespace(dependencies=dependencies)
    return await knowledge_run_route(
        session_id,
        request,
        SimpleNamespace(app=SimpleNamespace(state=state)),
    )


async def list_messages(session_id: str):
    state = SimpleNamespace(session_repository=repo)
    return await list_messages_route(
        session_id,
        SimpleNamespace(app=SimpleNamespace(state=state)),
    )


async def get_knowledge_document(document_id: str):
    state = SimpleNamespace(knowledge_repository=knowledge_repository)
    return await knowledge_document_route(
        document_id,
        SimpleNamespace(app=SimpleNamespace(state=state)),
    )