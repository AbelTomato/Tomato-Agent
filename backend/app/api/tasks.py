from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.tasks.models import SteerRequest, TaskActionRequest, TaskRecoveryObservation, UnifiedTaskRequest
from app.workspaces.service import WorkspacePathError

router = APIRouter()
PageLimit = Query(default=50, ge=1, le=200)


class EffectRiskAcceptanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    client_request_id: str = Field(min_length=1, max_length=200)


def ident(value, name="run_id"):
    try:
        return UUID(value)
    except ValueError as exc:
        raise HTTPException(400, {"code": f"invalid_{name}"}) from exc


def task_error(exc):
    # Public errors deliberately exclude paths, model inputs and configuration.
    code = str(exc)
    if isinstance(exc, KeyError):
        return HTTPException(404, {"code": "resource_not_found"})
    if code in {"worker_disabled", "model_unconfigured", "validation_backend_unavailable", "assistant_disabled"}:
        return HTTPException(503, {"code": code})
    if code in {"Registered Docker test executor required", "Original project tests require Docker"}:
        return HTTPException(503, {"code": "validation_backend_unavailable"})
    conflicts = {"session_busy", "workspace_busy", "workspace_busy_unknown_outcome", "request_conflict", "version_conflict",
                 "stale_action", "action_not_allowed", "confirmation_required", "workspace_changed",
                 "domain_version_conflict", "action_conflict", "action_request_conflict", "config_changed", "file_conflict",
                 "project_operation_exists", "session_mismatch", "effect_conflict_required", "risk_already_accepted",
                 "action_request_conflict"}
    if code in conflicts:
        return HTTPException(409, {"code": code})
    if isinstance(exc, WorkspacePathError):
        return HTTPException(409, {"code": "workspace_changed"})
    return HTTPException(422, {"code": "invalid_task"})


async def session_required(request, session_id):
    session = await request.app.state.session_repository.get_session(session_id)
    if session is None:
        raise HTTPException(404, {"code": "session_not_found"})
    return session


async def domain_required(request, run):
    await session_required(request, run.session_id)
    if run.domain_run_id:
        try:
            domain_id = UUID(run.domain_run_id)
            if run.task_type == "writing":
                domain = await request.app.state.writing_repository.get_task(domain_id)
            else:
                domain = await request.app.state.session_repository.get_run(domain_id)
            if domain is None or domain.session_id != run.session_id:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(409, {"code": "domain_link_invalid"}) from exc


async def task_required(request, run_id):
    run = await request.app.state.run_repository.get_run(ident(run_id))
    if run is None or run.session_id is None or run.task_type not in {"chat", "knowledge", "writing", "code", "assistant"}:
        raise HTTPException(404, {"code": "task_not_found"})
    await domain_required(request, run)
    return run


async def task_payload(request, run, *, history=False):
    await domain_required(request, run)
    snapshot = await request.app.state.execution_repository.get_snapshot(run.id)
    active = run.status in {"queued", "running", "waiting"}
    pending = request.app.state.task_service.pending_action(run) if active else None
    capabilities = list(pending.allowed_actions) if pending else (["cancel"] if active else [])
    payload = {"run_id": str(run.id), "session_id": str(run.session_id), "kind": run.task_type,
        "workspace_id": run.workspace_id, "status": run.status, "phase": snapshot.phase if snapshot else run.stage,
        "version": run.version, "result": run.result, "error": run.error,
        "pending_action": pending.model_dump(mode="json") if pending else None,
        "capabilities": capabilities, "created_at": run.created_at.isoformat(), "legacy": False}
    if run.task_type == "code":
        details = await request.app.state.code_task_service._result_details(str(run.id), include_events=False)
        payload["result"] = {key: details[key] for key in
            ("usage", "tool_calls", "artifacts", "test_results", "diff_artifact", "changed_files")}
    if history:
        payload["messages"] = await domain_messages(request, run.domain_run_id) if run.task_type in {"chat", "knowledge"} else []
    return payload


async def domain_messages(request, domain_id):
    if not domain_id:
        return []
    events = await request.app.state.session_repository.list_events(UUID(domain_id))
    return [{**event.payload, "role": "user" if event.event_type == "user_message" else "assistant"}
        for event in events if event.event_type == "user_message" or
        (event.event_type == "assistant_message" and not event.payload.get("tool_calls"))]


@router.post("/api/sessions/{session_id}/tasks", status_code=202)
async def submit_task(session_id: str, body: UnifiedTaskRequest, request: Request):
    session_id = ident(session_id, "session_id")
    await session_required(request, session_id)
    # Task 1 publishes the schema only. Assistant execution is installed by
    # the later capability/API slice, never through a legacy stage adapter.
    if body.kind == "assistant" and not request.app.state.config.assistant_enabled:
        raise task_error(RuntimeError("assistant_disabled"))
    try:
        run = await request.app.state.task_service.submit(session_id, body)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise task_error(exc) from exc
    return await task_payload(request, run)


@router.get("/api/tasks/{run_id}")
async def get_task(run_id: str, request: Request):
    return await task_payload(request, await task_required(request, run_id))


@router.post("/api/sessions/{session_id}/queue", status_code=202)
async def enqueue_task(session_id: str, body: UnifiedTaskRequest, request: Request):
    session_id = ident(session_id, "session_id")
    await session_required(request, session_id)
    try:
        return await request.app.state.task_service.enqueue(session_id, body)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise task_error(exc) from exc


@router.get("/api/sessions/{session_id}/queue")
async def session_queue(session_id: str, request: Request):
    session_id = ident(session_id, "session_id")
    await session_required(request, session_id)
    return await request.app.state.run_repository.list_queued_requests(session_id)


@router.post("/api/sessions/{session_id}/queue/resume")
async def resume_queue(session_id: str, request: Request):
    session_id = ident(session_id, "session_id")
    await session_required(request, session_id)
    return await request.app.state.run_repository.control_queue(session_id)


@router.post("/api/sessions/{session_id}/queue/clear")
async def clear_queue(session_id: str, request: Request):
    session_id = ident(session_id, "session_id")
    await session_required(request, session_id)
    return await request.app.state.run_repository.control_queue(session_id, clear=True)


@router.get("/api/tasks/{run_id}/events")
async def task_events(run_id: str, request: Request, after: int = Query(0, ge=0), limit: int = PageLimit):
    run = await task_required(request, run_id)
    events = await request.app.state.run_repository.list_events(run.id, after=after, limit=limit)
    return {"events": [event.model_dump(mode="json") for event in events],
            "next_cursor": events[-1].sequence if events else after}


@router.post("/api/tasks/{run_id}/actions")
async def task_action(run_id: str, body: TaskActionRequest, request: Request):
    run = await task_required(request, run_id)
    try:
        outcome = await request.app.state.task_service.act(run.id, body)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise task_error(exc) from exc
    payload = await task_payload(request, await task_required(request, run_id))
    if (run.task_type == "code" and body.action in {"inspect_recovery", "continue_recovery"}
            and isinstance(outcome, dict) and outcome.get("recovery_reason")):
        payload["recovery"] = TaskRecoveryObservation(
            version=payload["version"], reason=outcome["recovery_reason"],
            action=outcome["recovery_action"],
            allowed_actions=[action for action in payload["capabilities"]
                             if action in {"cancel", "inspect_recovery", "continue_recovery"}],
        ).model_dump(mode="json")
    return payload


@router.post("/api/tasks/{run_id}/steer", status_code=202)
async def steer_task(run_id: str, body: SteerRequest, request: Request):
    run = await task_required(request, run_id)
    try:
        return await request.app.state.execution_repository.request_steer(run.id, body)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise task_error(exc) from exc


@router.get("/api/tasks/{run_id}/effect-settlements")
async def effect_settlements(run_id: str, request: Request):
    run = await task_required(request, run_id)
    return {"settlements": await request.app.state.execution_repository.list_effect_settlements(run.id)}


@router.post("/api/tasks/{run_id}/effect-settlements/{invocation_id}/accept-risk")
async def accept_effect_conflict_risk(run_id: str, invocation_id: str,
                                     request: Request, body: EffectRiskAcceptanceRequest | None = None):
    run = await task_required(request, run_id)
    try:
        invocation = ident(invocation_id, "invocation_id")
        await request.app.state.execution_repository.accept_effect_conflict_risk(
            run.id, invocation, body.client_request_id if body else None)
    except (KeyError, ValueError, RuntimeError) as exc:
        raise task_error(exc) from exc
    return {"settlements": await request.app.state.execution_repository.list_effect_settlements(run.id)}


@router.get("/api/sessions/{session_id}/tasks")
async def task_history(session_id: str, request: Request, limit: int = PageLimit, offset: int = Query(0, ge=0)):
    session_id = ident(session_id, "session_id")
    await session_required(request, session_id)
    repo = request.app.state.session_repository
    rows = await repo.task_history_ids(session_id, limit, offset)
    items = []
    for run_id, source in rows:
        if source == "unified":
            items.append(await task_payload(request, await task_required(request, run_id), history=True))
            continue
        if source == "legacy_writing":
            domain = await request.app.state.writing_repository.get_task(UUID(run_id))
            result = domain.model_dump(mode="json")
            messages = []
        else:
            domain = await repo.get_run(UUID(run_id), session_id)
            messages = await domain_messages(request, run_id)
            result = messages[-1] if messages else None
        kind = "writing" if source == "legacy_writing" else (
            "knowledge" if any("citations" in message and "answer" in message for message in messages) else "chat")
        items.append({"run_id": run_id, "session_id": str(session_id), "kind": kind,
            "status": domain.status, "version": getattr(domain, "version", 0), "phase": None,
            "workspace_id": None, "result": result, "pending_action": None, "capabilities": [],
            "messages": messages, "created_at": domain.created_at.isoformat(), "legacy": True})
    return {"tasks": items, "limit": limit, "offset": offset}