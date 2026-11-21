"""Desktop-only, cursor-based access to in-memory Runtime traces."""

from fastapi import APIRouter, HTTPException, Query, Request

from app.observability.trace_buffer import MAX_TRACE_PAGE_SIZE


router = APIRouter()


@router.get("/api/desktop/traces")
async def runtime_traces(
    request: Request,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=MAX_TRACE_PAGE_SIZE, ge=1, le=MAX_TRACE_PAGE_SIZE),
):
    if not getattr(request.app.state, "desktop_connection_installed", False):
        raise HTTPException(404, {"code": "trace_endpoint_unavailable"})
    client = request.headers.get("X-Tomato-Client", "")
    if not client or client not in request.app.state.desktop_clients:
        raise HTTPException(403, {"code": "desktop_client_not_attached"})
    buffer = getattr(request.app.state, "trace_buffer", None)
    if buffer is None:
        raise HTTPException(503, {"code": "trace_buffer_unavailable"})
    return buffer.read_after(after, limit=limit)
