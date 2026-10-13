"""Durable input revision requests applied only at server-observed safe points."""
import uuid

import aiosqlite
from app.agent.models import Message
from app.runs.models import RunRepositoryConflict
from .models import parse_execution_snapshot


class AssistantSteerRepository:
    async def request_steer(self, run_id, request):
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await self._run_row(db, run_id)
            old = await (await db.execute("SELECT id,message,status FROM assistant_steer WHERE run_id=? AND client_request_id=?",
                (str(run_id), request.client_request_id))).fetchone()
            if old:
                if old[1] != request.message:
                    raise RunRepositoryConflict("request_conflict")
                return {"request_id": old[0], "status": old[2]}
            if row is None:
                raise KeyError(str(run_id))
            if row[1] != "assistant" or row[4] not in {"queued", "running", "waiting"}:
                raise RunRepositoryConflict("action_not_allowed")
            identity = str(uuid.uuid4())
            await db.execute("INSERT INTO assistant_steer(id,run_id,client_request_id,message) VALUES (?,?,?,?)",
                (identity, str(run_id), request.client_request_id, request.message))
            await self._assistant_recovery_event(db, run_id, "assistant.interrupt_requested", now, {"request_id": identity})
            await self._commit(db)
            return {"request_id": identity, "status": "requested"}

    async def apply_steer(self, lease, snapshot):
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            await db.execute("BEGIN IMMEDIATE")
            await self._validate_lease(db, lease, now)
            requests = await (await db.execute("SELECT id,message FROM assistant_steer WHERE run_id=? "
                "AND status='requested' ORDER BY sequence", (str(lease.run_id),))).fetchall()
            if not requests or snapshot.pending_action or snapshot.phase == "waiting":
                return None
            if await (await db.execute("SELECT 1 FROM execution_model_calls WHERE run_id=? AND status IN ('reserved','unknown')",
                (str(lease.run_id),))).fetchone():
                return None
            if await (await db.execute("SELECT 1 FROM execution_operations WHERE run_id=? AND status='running'",
                (str(lease.run_id),))).fetchone():
                return None
            raw = await (await db.execute("SELECT state FROM task_run_checkpoints WHERE run_id=?", (str(lease.run_id),))).fetchone()
            current = parse_execution_snapshot(raw[0])
            resolved = current.harness_state.get("structured_state", {}).get("side_effect_resolutions", {})
            unknown = await (await db.execute("SELECT id FROM execution_operations WHERE run_id=? AND status='unknown'",
                (str(lease.run_id),))).fetchall()
            if any(resolved.get(item[0]) not in {"executed", "not_executed"} for item in unknown):
                return None
            dynamic = dict(snapshot.harness_state.get("structured_state", {}))
            messages = list(snapshot.messages)
            for identity, message in requests:
                await self._assistant_recovery_event(db, lease.run_id, "assistant.safe_point_reached", now, {"request_id": identity})
                messages.append(Message(role="user", content=message))
                dynamic["task"] = message
                dynamic["input_revision"] = dynamic.get("input_revision", 0) + 1
                await self._assistant_recovery_event(db, lease.run_id, "assistant.input_revision_added", now,
                    {"request_id": identity, "revision": dynamic["input_revision"]})
                await db.execute("UPDATE assistant_steer SET status='applied' WHERE id=?", (identity,))
                await self._assistant_recovery_event(db, lease.run_id, "assistant.steer_applied", now, {"request_id": identity})
            if snapshot.pending_operation_id:
                cursor = await db.execute("UPDATE execution_operations SET status='failed' WHERE id=? AND status='prepared'",
                    (str(snapshot.pending_operation_id),))
                if cursor.rowcount:
                    await self._assistant_recovery_event(db, lease.run_id, "tool.abandoned", now,
                        {"reason": "input_revised_before_start"}, snapshot.pending_operation_id)
            saved = snapshot.model_copy(update={"phase": "ready_model", "messages": messages,
                "harness_state": {**snapshot.harness_state, "structured_state": dynamic},
                "logical_index": snapshot.logical_index + 1, "pending_response": None,
                "pending_decision": None, "pending_operation_id": None,
                "reserved_model_calls": max(current.reserved_model_calls, snapshot.reserved_model_calls),
                "elapsed_upper_bound_seconds": max(current.elapsed_upper_bound_seconds, snapshot.elapsed_upper_bound_seconds),
                "checkpoint_revision": current.checkpoint_revision + 1})
            await db.execute("UPDATE task_run_checkpoints SET state=?,sequence=sequence+1,created_at=? WHERE run_id=?",
                (saved.model_dump_json(), now.isoformat(), str(lease.run_id)))
            await self._commit(db)
            return saved