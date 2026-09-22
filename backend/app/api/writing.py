from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.writing.service import (
    WritingConflictError,
    WritingNotFoundError,
    WritingService,
)

router = APIRouter()


class CreateWritingTaskRequest(BaseModel):
    topic: str = Field(min_length=1)


class ConfirmOutlineRequest(BaseModel):
    version: int = Field(ge=1)
    outline: dict = Field(default_factory=dict)


class SaveWritingTaskRequest(BaseModel):
    version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1)


class RetryWritingTaskRequest(BaseModel):
    version: int = Field(ge=1)


def _service(request: Request) -> WritingService:
    return request.app.state.writing_service


@router.post("/api/sessions/{session_id}/writing-tasks")
async def create_writing_task(session_id: str, request: CreateWritingTaskRequest, http_request: Request):
    try:
        session_ident = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(400, "Invalid session_id") from exc
    try:
        task = await _service(http_request).create_task(session_ident, request.topic)
    except ValueError as exc:
        if str(exc).startswith("Session not found"):
            raise HTTPException(404, str(exc)) from exc
        raise HTTPException(400, str(exc)) from exc
    return task.model_dump(mode="json")


@router.get("/api/writing-tasks/{task_id}")
async def get_writing_task(task_id: str, http_request: Request):
    try:
        task_ident = UUID(task_id)
    except ValueError as exc:
        raise HTTPException(400, "Invalid task_id") from exc
    try:
        task = await _service(http_request).get_task(task_ident)
    except WritingNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return task.model_dump(mode="json")


@router.post("/api/writing-tasks/{task_id}/confirm-outline")
async def confirm_outline(task_id: str, request: ConfirmOutlineRequest, http_request: Request):
    try:
        task_ident = UUID(task_id)
    except ValueError as exc:
        raise HTTPException(400, "Invalid task_id") from exc
    try:
        task = await _service(http_request).confirm_outline(
            task_ident,
            expected_version=request.version,
            outline=request.outline,
        )
    except WritingNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except WritingConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    return task.model_dump(mode="json")


@router.post("/api/writing-tasks/{task_id}/save")
async def save_writing_task(task_id: str, request: SaveWritingTaskRequest, http_request: Request):
    try:
        task_ident = UUID(task_id)
    except ValueError as exc:
        raise HTTPException(400, "Invalid task_id") from exc
    try:
        task = await _service(http_request).save(
            task_ident,
            expected_version=request.version,
            idempotency_key=request.idempotency_key,
        )
    except WritingNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except WritingConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return task.model_dump(mode="json")


@router.post("/api/writing-tasks/{task_id}/retry")
async def retry_writing_task(task_id: str, request: RetryWritingTaskRequest, http_request: Request):
    try:
        task_ident = UUID(task_id)
    except ValueError as exc:
        raise HTTPException(400, "Invalid task_id") from exc
    try:
        task = await _service(http_request).retry(
            task_ident,
            expected_version=request.version,
        )
    except WritingNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except WritingConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    return task.model_dump(mode="json")
