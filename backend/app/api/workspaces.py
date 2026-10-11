from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.workspaces.models import WorkspaceRecord
from app.workspaces.service import WorkspacePathError, WorkspaceService

router = APIRouter()


class RegisterWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1)
    pytest_paths: list[str] = Field(min_length=1)


def _service(request: Request) -> WorkspaceService:
    return request.app.state.workspace_service


def _workspace_id(value: str) -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError
        return value
    except ValueError as exc:
        raise HTTPException(400, "Invalid workspace_id") from exc


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, "Workspace not found")
    if isinstance(exc, WorkspacePathError):
        if str(exc) == "workspace_changed":
            return HTTPException(409, {"code": "workspace_changed"})
        return HTTPException(403, {"code": "workspace_path_rejected"})
    return HTTPException(422, {"code": "invalid_workspace", "message": str(exc)})


@router.post("/api/workspaces", status_code=201, response_model=WorkspaceRecord)
async def register_workspace(body: RegisterWorkspaceRequest, request: Request):
    try:
        return await _service(request).register_original(Path(body.path), body.pytest_paths)
    except (ValueError, KeyError) as exc:
        raise _error(exc) from exc


@router.get("/api/workspaces/{workspace_id}", response_model=WorkspaceRecord)
async def get_workspace(workspace_id: str, request: Request):
    workspace_id = _workspace_id(workspace_id)
    try:
        return await _service(request).get(workspace_id)
    except (ValueError, KeyError) as exc:
        raise _error(exc) from exc


@router.post("/api/workspaces/{workspace_id}/unregister")
async def unregister_workspace(workspace_id: str, request: Request):
    workspace_id = _workspace_id(workspace_id)
    try:
        await _service(request).unregister(workspace_id)
    except (ValueError, KeyError) as exc:
        raise _error(exc) from exc
    return {"workspace_id": workspace_id, "status": "unregistered"}