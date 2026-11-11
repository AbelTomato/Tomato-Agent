import hashlib

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.memory.extraction import _SENSITIVE
from app.memory.models import (MemoryCandidateDecision, MemoryExplicitIntent, MemoryItemCreate,
                               MemoryItemPatch, MemoryRecallRequest, MemoryScope, MemorySourceInput,
                               MemoryTurnIngest)
from app.memory.repository import MemoryConflictError
from app.memory.retrieval import MemoryRetriever


router = APIRouter(prefix="/api/memory", tags=["memory"])


class MemoryStatus(BaseModel):
    protocol_version: int = 1
    enabled: bool = False
    ready: bool
    state: str
    generation: int


class SourceDeleteRequest(BaseModel):
    source_system: str
    source_installation_id: str
    session_id: str
    message_id: str
    source_revision: int


class ForgetMemoryRequest(BaseModel):
    expected_version: int


class ClearMemoryRequest(BaseModel):
    scope: MemoryScope
    scope_id: str
    expected_generation: int


@router.get("/status", response_model=MemoryStatus)
async def get_memory_status(request: Request) -> MemoryStatus:
    repository = request.app.state.memory_repository
    enabled = bool(request.app.state.config.memory_enabled)
    mode = request.app.state.config.memory_auto_mode
    extractor_ready = mode == "off" or request.app.state.memory_extractor is not None
    worker_ready = mode == "off" or request.app.state.memory_worker is not None
    ready = not enabled or (extractor_ready and worker_ready)
    return MemoryStatus(enabled=enabled, ready=ready,
                        state=("disabled" if not enabled else "ready" if ready else "degraded"),
                        generation=await repository.generation())


@router.post("/recall")
async def recall_memory(body: MemoryRecallRequest, request: Request):
    config = request.app.state.config
    if not config.memory_enabled:
        raise HTTPException(409, {"code": "memory_disabled"})
    return await MemoryRetriever(
        request.app.state.memory_repository,
        request.app.state.memory_embedding_client,
        embedding_model=(
            f"{config.embedding_base_url.rstrip('/')}|{config.embedding_model}|{config.embedding_dimensions}"
        ),
    ).recall(
        query=body.query,
        personal_scope_id=config.desktop_environment_id,
        project_scope_id=body.project_scope_id,
        session_scope_id=body.session_scope_id,
        limit=body.limit,
        budget_chars=body.budget_chars,
    )


@router.get("/contexts/{run_id}")
async def get_memory_contexts(run_id: str, request: Request):
    contexts = await request.app.state.memory_repository.list_contexts(run_id=run_id)
    scope_id = request.app.state.config.desktop_environment_id
    if any(context["scope_id"] != scope_id for context in contexts):
        raise HTTPException(404, {"code": "memory_context_not_found"})
    return {"contexts": contexts}


@router.post("/sources/ingest")
async def ingest_memory_source(body: MemoryTurnIngest, request: Request):
    config = request.app.state.config
    if not config.memory_enabled or config.memory_auto_mode == "off":
        raise HTTPException(409, {"code": "memory_extraction_disabled"})
    repository = request.app.state.memory_repository
    if request.app.state.memory_worker is None:
        raise HTTPException(503, {"code": "memory_worker_unavailable"})
    result = await repository.enqueue_turn(
        scope_id=config.desktop_environment_id,
        value=body.model_dump(mode="json"),
    )
    return result


@router.post("/intent")
async def process_explicit_memory_intent(body: MemoryExplicitIntent, request: Request):
    config = request.app.state.config
    if not config.memory_enabled:
        raise HTTPException(409, {"code": "memory_disabled"})
    if body.message.role != "user":
        raise HTTPException(422, {"code": "memory_source_must_be_user"})
    extractor = request.app.state.memory_extractor
    if extractor is None:
        raise HTTPException(503, {"code": "memory_extractor_unavailable"})
    if not body.message.content.strip() or _SENSITIVE.search(body.message.content):
        raise HTTPException(422, {"code": "memory_intent_not_eligible"})
    result = await extractor.extract([body.message.model_dump(mode="json")])
    facts = [fact for fact in result.facts if body.message.message_id in fact.evidence_message_ids]
    repository = request.app.state.memory_repository
    source = MemorySourceInput(
        source_system=body.source_system,
        source_installation_id=body.source_installation_id,
        session_id=body.session_id,
        message_id=body.message.message_id,
        source_revision=body.message.revision,
        role="user",
        message_at=body.message.message_at,
        excerpt=body.message.content[:1000],
    )
    if len(facts) != 1 or not facts[0].self_reported or _SENSITIVE.search(facts[0].content):
        candidate_ids = []
        for fact in facts:
            if not fact.self_reported or fact.confidence < 0.55:
                continue
            candidate_ids.append(await repository.add_candidate(
                scope_id=config.desktop_environment_id,
                payload={
                    "kind": fact.kind,
                    "topic_key": fact.topic_key,
                    "content": fact.content,
                    "confidence": fact.confidence,
                    "reason": "explicit_intent_ambiguous",
                    "conflicting_item_ids": [],
                    "sources": [source.model_dump()],
                },
            ))
        return {"status": "candidate" if candidate_ids else "not_saved",
                "candidate_ids": candidate_ids}
    fact = facts[0]
    identity = f"{body.source_system}:{body.source_installation_id}:{body.session_id}:{body.message.message_id}:{body.message.revision}"
    try:
        item = await repository.create(MemoryItemCreate(
            kind=fact.kind,
            scope="personal",
            scope_id=config.desktop_environment_id,
            topic_key=fact.topic_key,
            content=fact.content.strip(),
            confirmation="explicit",
            sources=[source],
        ), request_id=f"explicit:{hashlib.sha256(identity.encode()).hexdigest()}", supersede_topic=True)
    except ValueError as exc:
        raise HTTPException(409, {"code": str(exc)}) from exc
    return {"status": "saved", "item": item.model_dump()}


@router.get("/candidates")
async def list_memory_candidates(request: Request):
    return {"candidates": await request.app.state.memory_repository.list_candidates(
        scope_id=request.app.state.config.desktop_environment_id,
    )}


@router.post("/candidates/{candidate_id}/decision")
async def decide_memory_candidate(candidate_id: str, body: MemoryCandidateDecision, request: Request):
    try:
        return await request.app.state.memory_repository.decide_candidate(
            candidate_id,
            scope_id=request.app.state.config.desktop_environment_id,
            action=body.action,
        )
    except KeyError as exc:
        raise HTTPException(404, {"code": "candidate_not_found"}) from exc


@router.get("/items")
async def list_memory_items(request: Request, scope: MemoryScope = Query(...),
                            scope_id: str = Query(..., min_length=1, max_length=160),
                            limit: int = Query(100, ge=1, le=200),
                            offset: int = Query(0, ge=0)):
    if scope == "personal" and scope_id != request.app.state.config.desktop_environment_id:
        raise HTTPException(403, {"code": "memory_scope_forbidden"})
    try:
        items = await request.app.state.memory_repository.list_items(
            scope=scope, scope_id=scope_id, limit=limit, offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(422, {"code": str(exc)}) from exc
    return {"items": [item.model_dump() for item in items], "limit": limit, "offset": offset}


@router.post("/items")
async def create_memory_item(body: MemoryItemCreate, request: Request,
                             request_id: str = Query(..., min_length=1, max_length=160)):
    if body.scope == "personal" and body.scope_id != request.app.state.config.desktop_environment_id:
        raise HTTPException(403, {"code": "memory_scope_forbidden"})
    try:
        item = await request.app.state.memory_repository.create(body, request_id=request_id)
    except ValueError as exc:
        raise HTTPException(409, {"code": str(exc)}) from exc
    return item.model_dump()


@router.get("/items/{item_id}")
async def get_memory_item(item_id: str, request: Request):
    repository = request.app.state.memory_repository
    item = await repository.get(item_id)
    if item is None:
        raise HTTPException(404, {"code": "memory_not_found"})
    result = item.model_dump()
    result["sources"] = await repository.sources(item_id, item.version)
    return result


@router.get("/items/{item_id}/versions/{version}")
async def get_memory_item_version(item_id: str, version: int, request: Request):
    if version < 1:
        raise HTTPException(404, {"code": "memory_version_not_found"})
    repository = request.app.state.memory_repository
    item = await repository.get_version(item_id, version)
    if item is None:
        raise HTTPException(404, {"code": "memory_version_not_found"})
    if item.scope == "personal" and item.scope_id != request.app.state.config.desktop_environment_id:
        raise HTTPException(404, {"code": "memory_version_not_found"})
    result = item.model_dump()
    result["sources"] = await repository.sources(item_id, version)
    return result


@router.get("/export")
async def export_memory(request: Request):
    repository = request.app.state.memory_repository
    scope_id = request.app.state.config.desktop_environment_id
    items = []
    offset = 0
    while True:
        page = await repository.list_items(scope="personal", scope_id=scope_id, limit=200, offset=offset)
        for item in page:
            value = item.model_dump()
            value["sources"] = await repository.sources(item.item_id, item.version)
            items.append(value)
        if len(page) < 200:
            break
        offset += len(page)
    return {"scope": "personal", "scope_id": scope_id, "items": items}


@router.patch("/items/{item_id}")
async def correct_memory_item(item_id: str, body: MemoryItemPatch, request: Request,
                              request_id: str = Query(..., min_length=1, max_length=160)):
    try:
        item = await request.app.state.memory_repository.correct(
            item_id, content=body.content, reason=body.reason,
            expected_version=body.expected_version, request_id=request_id,
        )
    except KeyError as exc:
        raise HTTPException(404, {"code": "memory_not_found"}) from exc
    except MemoryConflictError as exc:
        raise HTTPException(409, {"code": "memory_version_conflict"}) from exc
    except ValueError as exc:
        raise HTTPException(409, {"code": str(exc)}) from exc
    return item.model_dump()


@router.post("/items/{item_id}/forget")
async def forget_memory_item(item_id: str, body: ForgetMemoryRequest, request: Request,
                             request_id: str = Query(..., min_length=1, max_length=160)):
    repository = request.app.state.memory_repository
    try:
        return await repository.forget(
            item_id, expected_version=body.expected_version, request_id=request_id,
        )
    except KeyError as exc:
        raise HTTPException(404, {"code": "memory_not_found"}) from exc
    except MemoryConflictError as exc:
        raise HTTPException(409, {"code": "memory_version_conflict"}) from exc
    except ValueError as exc:
        raise HTTPException(409, {"code": str(exc)}) from exc


@router.post("/sources/delete")
async def delete_memory_source(body: SourceDeleteRequest, request: Request,
                               request_id: str = Query(..., min_length=1, max_length=160)):
    try:
        return await request.app.state.memory_repository.delete_source(
            body.model_dump(), request_id=request_id,
        )
    except ValueError as exc:
        raise HTTPException(409, {"code": str(exc)}) from exc


@router.post("/clear")
async def clear_memory(body: ClearMemoryRequest, request: Request,
                       request_id: str = Query(..., min_length=1, max_length=160)):
    if body.scope == "personal" and body.scope_id != request.app.state.config.desktop_environment_id:
        raise HTTPException(403, {"code": "memory_scope_forbidden"})
    try:
        return await request.app.state.memory_repository.clear(
            scope=body.scope, scope_id=body.scope_id,
            expected_generation=body.expected_generation, request_id=request_id,
        )
    except MemoryConflictError as exc:
        raise HTTPException(409, {"code": "memory_generation_conflict"}) from exc
    except ValueError as exc:
        raise HTTPException(409, {"code": str(exc)}) from exc
