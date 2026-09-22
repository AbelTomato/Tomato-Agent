from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.agent.context import ContextManager
from app.agent.interfaces import LLMClient
from app.agent.runtime import AgentRuntime
from app.agent.models import Message, ToolDefinition, LLMResponse
from app.settings import Settings, settings
from app.sessions.repository import SessionRepository
from app.tools.calculator import Calculator
from app.tools.read_docs import ReadDocs
from app.tools.read_knowledge import ReadKnowledge
from app.tools.registry import ToolRegistry
from app.tools.search import Search
from app.tools.search_knowledge import SearchKnowledge
from app.llm.compatible_client import OpenAICompatibleClient
from app.knowledge.embeddings import EmbeddingClient
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.service import CitationSnapshot, KnowledgeAnswer, KnowledgePipeline, KnowledgeService
from app.knowledge.answerability import AnswerabilityConfig, CoverageAnswerabilityJudge
from app.knowledge.candidate_retrieval import RepositoryCandidateRetriever
from app.knowledge.evidence_selection import CoverageAwareEvidenceSelector
from app.knowledge.query_planning import LLMQueryPlanner, QueryPlannerConfig, SafeQueryPlanner
from app.knowledge.reranking import CompatibleReranker, NoopReranker
from app.api.writing import router as writing_router
from app.writing.repository import WritingRepository
from app.writing.service import WritingService

repo = SessionRepository(settings.database_path)
knowledge_repository = KnowledgeRepository(settings.database_path)
writing_repository = WritingRepository(settings.database_path)
registry = ToolRegistry(
    [
        Calculator(),
        Search(),
        ReadDocs(Path(settings.docs_root)),
        SearchKnowledge(knowledge_repository, max_result_chars=settings.max_tool_result_chars),
        ReadKnowledge(knowledge_repository, max_result_chars=settings.max_tool_result_chars),
    ]
)


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


def create_embedding_client(config: Settings | None = None) -> EmbeddingClient | None:
    config = config or settings
    if (
        not config.embedding_api_key.strip()
        or not config.embedding_model.strip()
        or config.embedding_dimensions <= 0
    ):
        return None
    return EmbeddingClient(
        api_key=config.embedding_api_key,
        base_url=config.embedding_base_url,
        model=config.embedding_model,
        dimensions=config.embedding_dimensions,
        timeout_seconds=config.embedding_timeout_seconds,
    )


def create_knowledge_service(
    repository: KnowledgeRepository,
    config: Settings | None = None,
) -> KnowledgeService:
    config = config or settings
    embedding_client = create_embedding_client(config)
    pipeline = None
    if config.knowledge_pipeline_enabled:
        planner = LLMQueryPlanner(
            QueryPlannerConfig(
                enabled=config.knowledge_query_planning_enabled,
                max_queries=config.knowledge_query_planning_max_queries,
                max_query_chars=config.knowledge_query_planning_max_query_chars,
            )
        ) if config.knowledge_query_planning_enabled else SafeQueryPlanner()
        candidate_retriever = RepositoryCandidateRetriever(
            repository,
            query_embedder=embedding_client.embed if embedding_client is not None else None,
            embedding_model=config.embedding_model,
            embedding_dimensions=config.embedding_dimensions,
            candidate_min_vector_similarity=config.knowledge_candidate_min_vector_similarity,
        )
        if config.knowledge_rerank_enabled:
            if not config.knowledge_rerank_api_key.strip():
                raise ValueError("knowledge rerank API key is required when rerank is enabled")
            reranker = CompatibleReranker(
                api_key=config.knowledge_rerank_api_key,
                base_url=config.knowledge_rerank_base_url,
                model=config.knowledge_rerank_model,
                timeout_seconds=config.knowledge_rerank_timeout_seconds,
            )
        else:
            reranker = NoopReranker()
        pipeline = KnowledgePipeline(
            planner=planner,
            candidate_retriever=candidate_retriever,
            reranker=reranker,
            selector=CoverageAwareEvidenceSelector(),
            judge=CoverageAnswerabilityJudge(),
            answerability_config=AnswerabilityConfig(
                min_supported_coverage=config.knowledge_answerability_min_supported_coverage,
                min_partial_coverage=config.knowledge_answerability_min_partial_coverage,
                min_supported_evidence=config.knowledge_answerability_min_supported_evidence,
                multi_evidence_requires_all_queries=config.knowledge_answerability_multi_evidence_requires_all_queries,
                allow_insufficient_llm=config.knowledge_answerability_allow_insufficient_llm,
            ),
        )
    return KnowledgeService(
        repository,
        query_embedder=embedding_client.embed if embedding_client is not None else None,
        embedding_model=config.embedding_model,
        embedding_dimensions=config.embedding_dimensions,
        min_vector_similarity=config.knowledge_min_vector_similarity,
        pipeline=pipeline,
        candidate_limit=config.knowledge_candidate_limit,
        allow_insufficient_llm=config.knowledge_answerability_allow_insufficient_llm,
    )


knowledge_service = create_knowledge_service(knowledge_repository)
writing_service = WritingService(writing_repository, draft_directory=settings.draft_directory)


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
    await knowledge_repository.init()
    await writing_repository.init()
    app.state.writing_service = writing_service
    yield


app = FastAPI(title="Tomato Agent Infrastructure", lifespan=lifespan)
app.state.writing_service = writing_service
app.include_router(writing_router)

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


class KnowledgeRunRequest(BaseModel):
    message: str
    retrieval_mode: str = "keyword"
    limit: int = Field(default=5, ge=1, le=20)


@app.get("/health")
async def health():
    return {"status": "ok", "runtime": "runtime-implemented"}


@app.get("/api/tools")
async def tools():
    return {"tools": [item.model_dump() for item in registry.definitions()]}


@app.get("/api/knowledge/capabilities")
async def knowledge_capabilities():
    embedding_ready = (
        getattr(knowledge_service, "query_embedder", None) is not None
        and bool(getattr(knowledge_service, "embedding_model", "").strip())
        and int(getattr(knowledge_service, "embedding_dimensions", 0)) > 0
    )
    retrieval_modes = ["keyword", "vector", "hybrid"] if embedding_ready else ["keyword"]
    return {
        "retrieval_modes": retrieval_modes,
        "embedding_configured": embedding_ready,
        "embedding_model": (
            getattr(knowledge_service, "embedding_model", None) if embedding_ready else None
        ),
        "embedding_dimensions": (
            getattr(knowledge_service, "embedding_dimensions", None) if embedding_ready else None
        ),
    }


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


@app.post("/api/sessions/{session_id}/knowledge-runs")
async def create_knowledge_run(session_id: str, request: KnowledgeRunRequest):
    if not request.message.strip():
        raise HTTPException(400, "message cannot be empty")
    try:
        ident = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(400, "Invalid session_id") from exc
    if not await repo.session_exists(ident):
        raise HTTPException(404, "Session not found")
    completed_events = await repo.list_completed_turn_events(ident)
    history = [
        {"role": event.payload.get("role", "user"), "content": event.payload.get("content", "")}
        for event in completed_events
        if event.event_type in {"user_message", "assistant_message"}
        and isinstance(event.payload.get("content", ""), str)
    ]
    retrieval_query = request.message
    run_id = await repo.create_run(ident)
    trace_id = uuid4()
    try:
        if history:
            retrieval_query = await knowledge_service.rewrite_query(
                request.message,
                history,
                llm_client=llm_client,
            )
        await repo.append_event(
            ident,
            run_id,
            "user_message",
            {
                "role": "user",
                "content": request.message,
                "original_query": request.message,
                "retrieval_query": retrieval_query,
                "retrieval_mode": request.retrieval_mode,
            },
        )
        answer_kwargs = {
            "mode": request.retrieval_mode,
            "limit": request.limit,
            "llm_client": llm_client,
        }
        if retrieval_query != request.message:
            answer_kwargs["retrieval_query"] = retrieval_query
        result = await knowledge_service.answer(request.message, **answer_kwargs)
    except ValueError as exc:
        await repo.update_run(run_id, "failed", {"error": str(exc)}, 0)
        raise HTTPException(400, str(exc)) from exc
    payload = result.model_dump(mode="json")
    await repo.append_event(
        ident,
        run_id,
        "assistant_message",
        {
            "role": "assistant",
            "content": payload["answer"],
            **payload,
        },
    )
    await repo.update_run(
        run_id,
        "completed",
        {"answer": payload["answer"], "citations": payload["citations"]},
        0,
    )
    return {
        "run_id": str(run_id),
        "session_id": str(ident),
        "status": "completed",
        "trace_id": str(trace_id),
        **payload,
    }


@app.get("/api/sessions/{session_id}/messages")
async def list_messages(session_id: str):
    try:
        ident = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(400, "Invalid session_id") from exc
    if not await repo.session_exists(ident):
        raise HTTPException(404, "Session not found")
    events = await repo.list_completed_turn_events(ident)
    messages = []
    for event in events:
        payload = dict(event.payload)
        payload.setdefault("role", "user" if event.event_type == "user_message" else "assistant")
        payload["run_id"] = str(event.run_id)
        messages.append(payload)
    return {"messages": messages}


@app.get("/api/knowledge/documents/{document_id}")
async def get_knowledge_document(document_id: str):
    document = await knowledge_repository.get_document(document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    chunks = await knowledge_repository.list_chunks(document_id)
    return {
        "document": {
            "document_id": document.document_id,
            "source_path": document.source_path,
            "source_url": document.source_url,
            "title": document.title,
            "document_version": document.document_version,
        },
        "chunks": [
            {
                "chunk_id": chunk.chunk_id,
                "document_version": chunk.document_version,
                "heading_path": chunk.heading_path,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "text": chunk.text,
                "token_count": chunk.token_count,
            }
            for chunk in chunks
        ],
    }
