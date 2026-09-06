from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.settings import settings
from app.sessions.repository import SessionRepository
from app.tools.calculator import Calculator
from app.tools.read_docs import ReadDocs
from app.tools.registry import ToolRegistry
from app.tools.search import Search

repo = SessionRepository(settings.database_path)
registry = ToolRegistry([Calculator(), Search(), ReadDocs(Path(settings.docs_root))])


@asynccontextmanager
async def lifespan(app: FastAPI):
    await repo.init()
    yield


app = FastAPI(title="Tomato Agent Infrastructure", lifespan=lifespan)


class CreateSessionRequest(BaseModel):
    metadata: dict = Field(default_factory=dict)


class RunRequest(BaseModel):
    message: str


@app.get("/health")
async def health():
    return {"status": "ok", "runtime": "user-implemented"}


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
    run_id = await repo.create_run(ident)
    await repo.append_event(ident, run_id, "user_message", {"message": request.message})
    await repo.update_run(
        run_id,
        "paused",
        {"reason": "Agent Runtime must be implemented by the user"},
    )
    return {
        "run_id": str(run_id),
        "session_id": session_id,
        "status": "paused",
        "message": "Implement Agent Runtime and connect it here.",
    }
