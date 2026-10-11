from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.tasks import ident, session_required, task_error

router = APIRouter()


class CreateSessionRequest(BaseModel):
    metadata: dict = Field(default_factory=dict)


class RunRequest(BaseModel):
    message: str


class SessionWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    workspace_id: str | None


@router.get("/api/sessions")
async def list_sessions(http_request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    sessions = await http_request.app.state.session_repository.list_sessions(limit, offset)
    return {"sessions": [session.model_dump(mode="json") for session in sessions], "limit": limit, "offset": offset}


@router.post("/api/sessions/{session_id}/workspace")
async def set_workspace(session_id: str, body: SessionWorkspaceRequest, http_request: Request):
    session_id = ident(session_id, "session_id")
    await session_required(http_request, session_id)
    if body.workspace_id is not None:
        try:
            await http_request.app.state.workspace_service.get(body.workspace_id)
        except (KeyError, ValueError) as exc:
            raise task_error(exc) from exc
    session = await http_request.app.state.session_repository.set_default_workspace(session_id, body.workspace_id)
    return session.model_dump(mode="json")


@router.post("/api/sessions")
async def create_session(request: CreateSessionRequest, http_request: Request):
    session_id = await http_request.app.state.session_repository.create_session(
        request.metadata
    )
    return {"session_id": str(session_id)}


@router.post("/api/sessions/{session_id}/runs")
async def create_run(session_id: str, request: RunRequest, http_request: Request):
    runtime = http_request.app.state.runtime
    try:
        result = await runtime.run(session_id, request.message)
    except ValueError as exc:
        message = str(exc)
        status_code = 404 if message.startswith("Session not found") else 400
        raise HTTPException(status_code, message) from exc
    return result.model_dump(mode="json")


@router.get("/api/sessions/{session_id}/messages")
async def list_messages(session_id: str, http_request: Request):
    try:
        ident = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(400, "Invalid session_id") from exc
    repository = http_request.app.state.session_repository
    if not await repository.session_exists(ident):
        raise HTTPException(404, "Session not found")
    events = await repository.list_completed_turn_events(ident)
    messages = []
    for event in events:
        payload = dict(event.payload)
        payload.setdefault(
            "role", "user" if event.event_type == "user_message" else "assistant"
        )
        payload["run_id"] = str(event.run_id)
        messages.append(payload)
    return {"messages": messages}