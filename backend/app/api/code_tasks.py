from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from app.agent.code_task import CodeTaskService
from app.agent.models import CodeTaskRequest
from app.artifacts.service import ArtifactNotFoundError

router = APIRouter()


class CancelCodeTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reason: str = "cancelled"


def _service(request: Request) -> CodeTaskService:
    return request.app.state.code_task_service


def _run_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise HTTPException(400, "Invalid run_id") from exc


async def _run_payload(service: CodeTaskService, run_id: str) -> dict:
    run = await service.run_repository.get_run(run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    details = await service._result_details(run_id)
    return {"run_id": str(run.id), "task_type": run.task_type,
            "workspace_id": run.workspace_id, "status": run.status,
            "error": run.error, "version": run.version, **details}


@router.post("/api/code-tasks", status_code=status.HTTP_201_CREATED)
async def create_code_task(request: CodeTaskRequest, http_request: Request):
    run = await _service(http_request).create(request)
    return {"run_id": str(run.id), "workspace_id": run.workspace_id,
            "status": run.status, "task_type": run.task_type}


@router.post("/api/code-tasks/{run_id}/execute")
async def execute_code_task(run_id: str, http_request: Request):
    run_id = _run_id(run_id)
    service = _service(http_request)
    run = await service.run_repository.get_run(run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    if not getattr(http_request.app.state, "code_task_llm", None):
        await service.run_repository.update_status(run_id, "failed", error={"code": "model_unconfigured"})
        raise HTTPException(503, {"code": "model_unconfigured"})
    result = await service.execute(
        run_id,
        llm=http_request.app.state.code_task_llm,
        strategy=http_request.app.state.code_task_strategy,
        test_tool=getattr(http_request.app.state, "code_task_test_tool", None),
    )
    if result.get("status") == "failed":
        code = result.get("error", {}).get("code", "execution_failed")
        raise HTTPException(409, {"code": code, "result": result})
    return result


@router.get("/api/code-tasks/{run_id}")
async def get_code_task(run_id: str, http_request: Request):
    return await _run_payload(_service(http_request), _run_id(run_id))


@router.get("/api/code-tasks/{run_id}/events")
async def list_code_task_events(run_id: str, http_request: Request):
    run_id = _run_id(run_id)
    service = _service(http_request)
    if await service.run_repository.get_run(run_id) is None:
        raise HTTPException(404, "Run not found")
    events = await service.run_repository.list_events(run_id)
    return {"events": [event.model_dump(mode="json") for event in events]}


@router.post("/api/code-tasks/{run_id}/cancel")
async def cancel_code_task(run_id: str, request: CancelCodeTaskRequest, http_request: Request):
    run_id = _run_id(run_id)
    try:
        run = await _service(http_request).cancel(run_id, request.reason)
    except KeyError as exc:
        raise HTTPException(404, "Run not found") from exc
    return {"run_id": str(run.id), "status": run.status, "error": run.error}


@router.get("/api/code-tasks/{run_id}/artifacts/{artifact_id}")
async def get_code_task_artifact(run_id: str, artifact_id: str, http_request: Request):
    run_id = _run_id(run_id)
    service = _service(http_request)
    if await service.run_repository.get_run(run_id) is None:
        raise HTTPException(404, "Run not found")
    try:
        ref = await service.artifact_service.get(artifact_id)
        if str(ref.run_id) != run_id:
            raise ArtifactNotFoundError(artifact_id)
        content = await service.artifact_service.read(artifact_id, service.artifact_service.max_bytes)
    except (ArtifactNotFoundError, ValueError):
        raise HTTPException(404, "Artifact not found")
    return {"artifact": ref.model_dump(mode="json"), "content": content.decode("utf-8", errors="replace")}