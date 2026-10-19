from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.projects.service import ProjectPathError, ProjectService

router = APIRouter(prefix="/api/projects", tags=["projects"])


class OpenProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1)


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    parent_path: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=255)


def _service(request: Request) -> ProjectService:
    return request.app.state.project_service


def _raise(exc: Exception):
    if isinstance(exc, FileExistsError):
        raise HTTPException(409, {"code": "project_exists"}) from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(403, {"code": "project_permission_denied"}) from exc
    if isinstance(exc, ProjectPathError):
        raise HTTPException(403, {"code": "project_path_rejected", "message": str(exc)}) from exc
    raise HTTPException(422, {"code": "invalid_project", "message": str(exc)}) from exc


@router.get("")
async def list_projects(request: Request):
    return {"projects": await _service(request).list()}


@router.get("/directories")
async def browse_directories(request: Request, path: str, include_hidden: bool = False):
    try:
        return await _service(request).browse(Path(path), include_hidden=include_hidden)
    except (OSError, ValueError) as exc:
        _raise(exc)


@router.post("/open", status_code=201)
async def open_project(body: OpenProjectRequest, request: Request, response: Response):
    service = _service(request)
    await service.initialize()
    existing = await service.repository.get_for_path(service.environment_id, service.service_instance_id,
                                                     str(Path(body.path).resolve()))
    try:
        record = await service.open(Path(body.path))
        if existing is not None:
            response.status_code = 200
        return record
    except (OSError, ValueError) as exc:
        _raise(exc)


@router.post("", status_code=201)
async def create_project(body: CreateProjectRequest, request: Request):
    try:
        return await _service(request).create(Path(body.parent_path), body.name)
    except (OSError, ValueError) as exc:
        _raise(exc)