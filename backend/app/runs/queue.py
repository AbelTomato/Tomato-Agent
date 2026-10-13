"""Session requests are durable identities, distinct from executable Runs."""
import json
from datetime import datetime, timezone
from uuid import uuid4

import aiosqlite

from .models import RunRepositoryConflict


class SessionQueueRepository:
    async def enqueue_request(self, session_id, request):
        from app.execution.ports import canonical_digest
        payload = request.model_dump(mode="json")
        digest = canonical_digest(payload)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            if not await (await db.execute("SELECT 1 FROM sessions WHERE id=?", (str(session_id),))).fetchone():
                raise KeyError("session_not_found")
            old = await (await db.execute("SELECT id,digest,status,run_id FROM session_task_queue "
                "WHERE session_id=? AND client_request_id=?", (str(session_id), request.client_request_id))).fetchone()
            if old:
                if old[1] != digest:
                    raise RunRepositoryConflict("request_conflict")
                return {"request_id": old[0], "status": old[2], "run_id": old[3]}
            if await (await db.execute("SELECT 1 FROM task_runs WHERE session_id=? AND client_request_id=?",
                    (str(session_id), request.client_request_id))).fetchone():
                raise RunRepositoryConflict("request_conflict")
            identity = str(uuid4())
            await db.execute("INSERT INTO session_task_queue(id,session_id,client_request_id,request,digest,created_at) "
                "VALUES (?,?,?,?,?,?)", (identity, str(session_id), request.client_request_id,
                    json.dumps(payload), digest, datetime.now(timezone.utc).isoformat()))
            await db.commit()
            return {"request_id": identity, "status": "pending", "run_id": None}

    async def list_queued_requests(self, session_id):
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute("SELECT id,status,run_id,request,created_at FROM session_task_queue "
                "WHERE session_id=? ORDER BY sequence", (str(session_id),))).fetchall()
            latest = await (await db.execute("SELECT id,status FROM task_runs WHERE session_id=? "
                "ORDER BY created_at DESC,id DESC LIMIT 1", (str(session_id),))).fetchone()
            control = await (await db.execute("SELECT paused,acknowledged_run_id FROM session_queue_control WHERE session_id=?",
                (str(session_id),))).fetchone()
        paused = bool(control and control[0]) or bool(latest and latest[1] in {"failed", "cancelled", "timed_out"}
            and (not control or control[1] != latest[0]))
        return {"paused": paused, "requests": [{"request_id": row[0], "status": row[1], "run_id": row[2],
            "request": json.loads(row[3]), "created_at": row[4]} for row in rows]}

    async def control_queue(self, session_id, *, clear=False):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            latest = await (await db.execute("SELECT id FROM task_runs WHERE session_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (str(session_id),))).fetchone()
            if clear:
                await db.execute("UPDATE session_task_queue SET status='cancelled' WHERE session_id=? AND status='pending'",
                    (str(session_id),))
            else:
                await db.execute("INSERT INTO session_queue_control(session_id,paused,acknowledged_run_id) VALUES (?,0,?) "
                    "ON CONFLICT(session_id) DO UPDATE SET paused=0,acknowledged_run_id=excluded.acknowledged_run_id",
                    (str(session_id), latest[0] if latest else None))
            await db.commit()
        return await self.list_queued_requests(session_id)

    async def pending_queue_heads(self):
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute("SELECT q.id,q.session_id,q.request FROM session_task_queue q "
                "WHERE q.status='pending' AND q.sequence=(SELECT MIN(sequence) FROM session_task_queue "
                "WHERE session_id=q.session_id AND status='pending') ORDER BY q.sequence")).fetchall()
        return [(row[0], row[1], json.loads(row[2])) for row in rows]