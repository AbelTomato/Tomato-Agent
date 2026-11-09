"""Desktop-only authentication and conservative service ownership.

Client loss never cancels a Run. Unknown/stale clients deliberately prevent an
automatic shutdown; only an explicit close removes a connection.
"""

import asyncio
import hmac
from collections.abc import Callable

import aiosqlite
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def install_desktop(app: FastAPI, *, token: str, instance: str,
                    owner: str | None, stop: Callable[[], None]) -> None:
    app.state.desktop_connection_installed = True
    clients: set[str] = set()
    gate = asyncio.Lock()
    closing = False
    pending_owner_shutdown = False

    async def facts():
        async with aiosqlite.connect(app.state.config.database_path) as db:
            row = await (await db.execute(
                "SELECT COUNT(*) FROM task_runs WHERE status IN ('queued','running','waiting')"
            )).fetchone()
        return {"protocol": 1, "service_instance_id": instance,
                "connections": len(clients), "active_tasks": row[0]}

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        nonlocal closing
        supplied = request.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(supplied, f"Bearer {token}".encode()):
            return JSONResponse({"detail": {"code": "desktop_unauthorized"}}, status_code=401)
        if request.headers.get("X-Tomato-Instance") != instance:
            return JSONResponse({"detail": {"code": "desktop_instance_mismatch"}}, status_code=409)
        # Serializes admission and the idle check: a submission admitted before
        # close must be committed before close observes whether shutdown is safe.
        async with gate:
            if closing:
                return JSONResponse({"detail": {"code": "desktop_stopping"}}, status_code=503)
            return await call_next(request)

    @app.get("/api/desktop/connection")
    async def connection():
        return await facts()

    @app.post("/api/desktop/attach")
    async def attach(request: Request):
        nonlocal pending_owner_shutdown
        client = request.headers.get("X-Tomato-Client", "")
        if not client or len(client) > 128:
            return JSONResponse({"detail": {"code": "desktop_client_required"}}, status_code=400)
        pending_owner_shutdown = False
        clients.add(client)
        return await facts()

    async def shutdown_when_idle():
        nonlocal closing, pending_owner_shutdown
        while True:
            await asyncio.sleep(1)
            async with gate:
                if not pending_owner_shutdown:
                    return
                result = await facts()
                if result["active_tasks"] == 0 and not clients:
                    pending_owner_shutdown = False
                    closing = True
                    stop()
                    return

    @app.post("/api/desktop/close")
    async def close(request: Request):
        nonlocal closing, pending_owner_shutdown
        client = request.headers.get("X-Tomato-Client", "")
        if client not in clients:
            return JSONResponse({"detail": {"code": "desktop_client_not_attached"}}, status_code=409)
        clients.remove(client)
        result = await facts()
        supplied = request.headers.get("X-Tomato-Owner", "").encode()
        owner_close = bool(owner and hmac.compare_digest(supplied, owner.encode()) and not clients)
        shutdown = owner_close and result["active_tasks"] == 0
        shutdown_pending = owner_close and result["active_tasks"] > 0
        if shutdown:
            closing = True
            stop()
        elif shutdown_pending and not pending_owner_shutdown:
            pending_owner_shutdown = True
            asyncio.create_task(shutdown_when_idle())
        return {**result, "shutdown": shutdown, "shutdown_pending": shutdown_pending}
