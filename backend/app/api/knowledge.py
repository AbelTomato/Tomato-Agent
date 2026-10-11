from uuid import UUID

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

    from app.knowledge.conversation import KnowledgeConversation

    conversation = KnowledgeConversation(
        repository, http_request.app.state.knowledge_service, http_request.app.state.llm_client)
    try:
        return await conversation.run(ident, request.message,
            retrieval_mode=request.retrieval_mode, limit=request.limit)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc



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