from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()


class KnowledgeRunRequest(BaseModel):
    message: str
    retrieval_mode: str = "keyword"
    limit: int = Field(default=5, ge=1, le=20)


@router.get("/api/knowledge/capabilities")
async def knowledge_capabilities(request: Request):
    service = request.app.state.knowledge_service
    embedding_configured = bool(
        service.query_embedder
        and service.embedding_model
        and service.embedding_dimensions > 0
    )
    modes = ["keyword", "vector", "hybrid"] if embedding_configured else ["keyword"]
    return {
        "retrieval_modes": modes,
        "embedding_configured": embedding_configured,
        "embedding_model": service.embedding_model if embedding_configured else None,
        "embedding_dimensions": (
            service.embedding_dimensions if embedding_configured else None
        ),
    }


@router.post("/api/sessions/{session_id}/knowledge-runs")
async def create_knowledge_run(
    session_id: str,
    request: KnowledgeRunRequest,
    http_request: Request,
):
    try:
        ident = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(400, "Invalid session_id") from exc

    repository = http_request.app.state.session_repository
    if not await repository.session_exists(ident):
        raise HTTPException(404, "Session not found")

    history = [
        {
            "role": event.payload.get("role", "user"),
            "content": event.payload.get("content", ""),
        }
        for event in await repository.list_completed_turn_events(ident)
        if event.event_type in {"user_message", "assistant_message"}
        and isinstance(event.payload.get("content", ""), str)
    ]
    retrieval_query = request.message
    run_id = await repository.create_run(ident)
    trace_id = uuid4()
    service = http_request.app.state.knowledge_service
    llm_client = http_request.app.state.llm_client
    try:
        if history:
            retrieval_query = await service.rewrite_query(
                request.message,
                history,
                llm_client=llm_client,
            )
        await repository.append_event(
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
        result = await service.answer(request.message, **answer_kwargs)
    except ValueError as exc:
        await repository.update_run(run_id, "failed", {"error": str(exc)}, 0)
        raise HTTPException(400, str(exc)) from exc

    payload = result.model_dump(mode="json")
    await repository.append_event(
        ident,
        run_id,
        "assistant_message",
        {"role": "assistant", "content": payload["answer"], **payload},
    )
    await repository.update_run(
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


@router.get("/api/knowledge/documents/{document_id}")
async def get_knowledge_document(document_id: str, request: Request):
    repository = request.app.state.knowledge_repository
    document = await repository.get_document(document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    chunks = await repository.list_chunks(document_id)
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