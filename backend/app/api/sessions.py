from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()


class CreateSessionRequest(BaseModel):
    metadata: dict = Field(default_factory=dict)


class RunRequest(BaseModel):
    message: str


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