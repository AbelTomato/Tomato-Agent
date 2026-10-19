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


class SessionProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    project_id: str | None


class SessionTitleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=200)
    expected_version: int = Field(ge=0)


@router.get("/api/sessions")
async def list_sessions(http_request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    sessions = await http_request.app.state.session_repository.list_sessions(limit, offset)
    items = []
    projects = await http_request.app.state.project_service.list()
    by_workspace = {item["workspace_id"]: item["project_id"] for item in projects}
    for session in sessions:
        item = session.model_dump(mode="json")
        item["title"] = session.metadata.get("title") or "未命名对话"
        item["project_id"] = session.project_id or by_workspace.get(session.default_workspace_id)
        items.append(item)
    return {"sessions": items, "limit": limit, "offset": offset}


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


@router.patch("/api/sessions/{session_id}/project")
async def set_project(session_id: str, body: SessionProjectRequest, http_request: Request):
    session_id = ident(session_id, "session_id")
    await session_required(http_request, session_id)
    if body.project_id is not None:
        projects = await http_request.app.state.project_service.list()
        project = next((item for item in projects if item["project_id"] == body.project_id and item["available"]), None)
        if project is None:
            raise HTTPException(404, {"code": "project_not_found"})
    session = await http_request.app.state.session_repository.set_project(session_id, body.project_id)
    result = session.model_dump(mode="json")
    result["project_id"] = body.project_id
    return result


@router.patch("/api/sessions/{session_id}/title")
async def rename_session(session_id: str, body: SessionTitleRequest, http_request: Request):
    session_id = ident(session_id, "session_id")
    try:
        session = await http_request.app.state.session_repository.set_title(
            session_id, body.title, expected_version=body.expected_version
        )
    except KeyError as exc:
        raise HTTPException(404, {"code": "session_not_found"}) from exc
    except ValueError as exc:
        code = "title_version_conflict" if str(exc) == "title_version_conflict" else "invalid_title"
        raise HTTPException(409 if code == "title_version_conflict" else 422, {"code": code}) from exc
    result = session.model_dump(mode="json")
    result["title"] = session.metadata["title"]
    return result


@router.post("/api/sessions")
async def create_session(request: CreateSessionRequest, http_request: Request):
    metadata = dict(request.metadata)
    if not metadata.get("title"):
        metadata["title"] = "未命名对话"
    session_id = await http_request.app.state.session_repository.create_session(
        metadata
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