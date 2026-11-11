from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.layered_memory import _classify_project, _project_key


router = APIRouter(prefix="/api/layered-memory", tags=["layered-memory"])


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=3000)
    project_id: str | None = Field(default=None, max_length=160)


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: str = Field(min_length=1, max_length=160)
    turn_id: str = Field(min_length=1, max_length=160)
    user_content: str = Field(min_length=1, max_length=6000)
    assistant_content: str = Field(min_length=1, max_length=6000)
    occurred_at: str = Field(min_length=1, max_length=80)
    project_id: str | None = Field(default=None, max_length=160)


class RestoreProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=1)


@router.get("/profile")
async def get_profile(request: Request):
    if not request.app.state.config.memory_enabled:
        raise HTTPException(409, {"code": "memory_disabled"})
    profile = await request.app.state.layered_memory_repository.profile()
    return {"profile": profile}


@router.get("/profiles")
async def list_profiles(request: Request):
    if not request.app.state.config.memory_enabled:
        raise HTTPException(409, {"code": "memory_disabled"})
    return {"profiles": await request.app.state.layered_memory_repository.profiles()}


@router.post("/session/{session_id}/flush", status_code=202)
async def flush_session(session_id: str, request: Request):
    if not request.app.state.config.memory_enabled:
        raise HTTPException(409, {"code": "memory_disabled"})
    if request.app.state.layered_memory_worker is None:
        raise HTTPException(503, {"code": "layered_memory_worker_unavailable"})
    if not await request.app.state.session_repository.session_exists(session_id):
        raise HTTPException(404, {"code": "session_not_found"})
    await request.app.state.layered_memory_repository.queue_session(session_id)
    return {"status": "queued"}


@router.get("/episodes")
async def list_episodes(request: Request):
    if not request.app.state.config.memory_enabled:
        raise HTTPException(409, {"code": "memory_disabled"})
    episodes = await request.app.state.layered_memory_repository.episodes(100)
    return {"episodes": [{key: item[key] for key in
                          ("episode_id", "content", "occurred_at", "source_refs")} for item in episodes]}


@router.post("/profile/restore")
async def restore_profile(body: RestoreProfileRequest, request: Request):
    if not request.app.state.config.memory_enabled:
        raise HTTPException(409, {"code": "memory_disabled"})
    try:
        profile = await request.app.state.layered_memory_repository.restore_profile(body.version)
    except KeyError as exc:
        raise HTTPException(404, {"code": str(exc)}) from exc
    return {"profile": profile}


@router.post("/context")
async def get_context(body: ContextRequest, request: Request):
    if not request.app.state.config.memory_enabled:
        raise HTTPException(409, {"code": "memory_disabled"})
    service = request.app.state.layered_memory_service
    project_key = await _resolve_project_key(request, body.project_id)
    try:
        result = await service.context(body.query, body.project_id, project_key)
    except Exception:
        profile = await request.app.state.layered_memory_repository.profile()
        result = {"profile": profile, "episodes": [], "retrieval": "profile_only_embedding_unavailable"}
    return result


@router.post("/turn", status_code=202)
async def observe_turn(body: TurnRequest, request: Request):
    if not request.app.state.config.memory_enabled:
        raise HTTPException(409, {"code": "memory_disabled"})
    service = request.app.state.layered_memory_service
    if service.worker is None:
        raise HTTPException(503, {"code": "layered_memory_worker_unavailable"})
    values = body.model_dump()
    values["project_key"] = await _resolve_project_key(request, body.project_id)
    await service.observe_turn(**values)
    return {"status": "accepted"}


@router.post("/distill")
async def distill_profile(request: Request):
    if not request.app.state.config.memory_enabled:
        raise HTTPException(409, {"code": "memory_disabled"})
    worker = request.app.state.layered_memory_worker
    if worker is None:
        raise HTTPException(503, {"code": "layered_memory_worker_unavailable"})
    return {"profile": await worker.distill_now()}


async def _resolve_project_key(request: Request, project_id: str | None):
    if project_id is None:
        return None
    projects = await request.app.state.project_service.list_projects()
    project = next((item for item in projects if item["project_id"] == project_id), None)
    if project is None:
        return f"id:{project_id}"
    name = project.get("display_name")
    return _classify_project(name) or _project_key(name) or f"id:{project_id}"
