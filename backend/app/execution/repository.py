from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import aiosqlite

from app.runs.models import RunRecord, RunRepositoryConflict
from app.runs.repository import _RUN_COLUMNS, _json_safe, run_record_from_row
from app.runs.state_machine import transition_run

from .models import (AttemptRecord, LeaseHandle, PROTOCOL_VERSION, ExecutionSnapshot,
                     OperationInput, StepRecord, StepOutcome)
from .ports import Clock, canonical_digest, execution_migration
from .state_machine import StaleExecutionOwner
from .recovery import RecoveryCandidate, RecoveryConflict, RecoveryOperation, classify_recovery
from .steer import AssistantSteerRepository
from .settlement import AssistantSettlementRepository


class ExecutionRepository(AssistantSteerRepository, AssistantSettlementRepository):
    def __init__(self, path: Path, *, clock: Clock | None = None) -> None:
        self.path = Path(path)
        self.clock = clock or _SystemClock()

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            await db.executescript(";".join(execution_migration()) + ";")
            await db.executescript(_RECOVERY_SCHEMA)
            from app.runs.migrations import MODEL_CALL_SCHEMA
            await db.executescript(MODEL_CALL_SCHEMA)
            await db.execute("CREATE TABLE IF NOT EXISTS task_actions (run_id TEXT NOT NULL REFERENCES task_runs(id), "
                             "client_request_id TEXT NOT NULL, digest TEXT NOT NULL, "
                             "PRIMARY KEY(run_id,client_request_id))")
            await db.execute("CREATE TABLE IF NOT EXISTS assistant_steer (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
                "id TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL REFERENCES task_runs(id), client_request_id TEXT NOT NULL, "
                "message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'requested', UNIQUE(run_id,client_request_id))")
            await db.execute("CREATE TABLE IF NOT EXISTS assistant_effect_settlements ("
                "invocation_id TEXT PRIMARY KEY REFERENCES execution_operations(id), resolution TEXT NOT NULL, "
                "quarantined INTEGER NOT NULL DEFAULT 1, termination_evidence TEXT, conflict INTEGER NOT NULL DEFAULT 0)")
            await db.commit()

    async def claim_next(self, owner_id: str, *, lease_seconds: int) -> tuple[RunRecord, LeaseHandle] | None:
        """Atomically claim the oldest queued run, retrying if another worker wins."""
        while True:
            async with aiosqlite.connect(self.path) as db:
                await self._configure(db)
                row = await (await db.execute(
                    "SELECT r.id FROM task_runs r LEFT JOIN execution_run_heads h ON h.run_id=r.id "
                    "WHERE r.status='queued' AND (r.task_type IN ('code_task','code') OR "
                    "(r.task_type IN ('chat','knowledge','writing') AND EXISTS "
                    "(SELECT 1 FROM task_run_checkpoints c WHERE c.run_id=r.id "
                    "AND json_extract(c.state,'$.snapshot_type')='stage')) OR "
                    "(r.task_type='assistant' AND EXISTS (SELECT 1 FROM task_run_checkpoints c "
                    "WHERE c.run_id=r.id AND json_extract(c.state,'$.snapshot_type')='assistant'))) "
                    "AND (h.run_id IS NULL OR h.active_attempt_id IS NULL) "
                    "ORDER BY r.created_at, r.id LIMIT 1"
                )).fetchone()
            if row is None:
                return None
            run_id = UUID(row[0])
            try:
                lease = await self.claim(run_id, owner_id, lease_seconds=lease_seconds, queued_only=True)
            except RecoveryConflict:
                continue
            run = await self.get_run(run_id)
            if run is not None:
                return run, lease

    async def claim(
        self, run_id: UUID, owner_id: str, *, lease_seconds: int, queued_only: bool = False
    ) -> LeaseHandle:
        now = self._aware_now()
        expires = now + timedelta(seconds=lease_seconds)
        attempt_id = uuid4()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            try:
                await db.execute("BEGIN IMMEDIATE")
                run = await self._run_row(db, run_id)
                if run is None:
                    raise KeyError(str(run_id))
                if queued_only and run[4] != "queued":
                    raise RecoveryConflict("run_not_queued")
                if run[4] in {"completed", "failed", "cancelled", "timed_out"}:
                    raise RecoveryConflict("terminal_run")
                head = await self._head_row(db, run_id)
                if head is not None and head[1] is None and run[4] == "running":
                    raise RecoveryConflict("recovery_decision_required")
                if run[4] == "waiting" and head is not None:
                    attempt = await (await db.execute(
                        "SELECT status FROM execution_attempts WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1",
                        (str(run_id),))).fetchone()
                    if attempt and attempt[0] == "expired":
                        raise RecoveryConflict("manual_recovery_required")
                if head is not None and head[1] is not None:
                    raise RuntimeError("run is already claimed")
                token = (head[0] if head else 0) + 1
                attempt_no = await self._next_attempt_no(db, run_id)
                await db.execute(
                    "INSERT INTO execution_run_heads "
                    "(run_id, active_attempt_id, fencing_token, cancel_requested, protocol_version) "
                    "VALUES (?, ?, ?, 0, ?) "
                    "ON CONFLICT(run_id) DO UPDATE SET active_attempt_id=excluded.active_attempt_id, "
                    "fencing_token=excluded.fencing_token, cancel_requested=0, "
                    "protocol_version=excluded.protocol_version",
                    (str(run_id), str(attempt_id), token, PROTOCOL_VERSION),
                )
                await db.execute(
                    "INSERT INTO execution_attempts "
                    "(id, run_id, attempt_no, owner_id, fencing_token, status, lease_expires_at, "
                    "heartbeat_at, started_at) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)",
                    (str(attempt_id), str(run_id), attempt_no, owner_id, token,
                     _dump_time(expires), _dump_time(now), _dump_time(now)),
                )
                await self._set_run_status(db, run_id, "running", now)
                await db.execute("UPDATE task_run_checkpoints SET created_at=? WHERE run_id=? "
                    "AND json_extract(state,'$.snapshot_type')='assistant'", (_dump_time(now), str(run_id)))
                await self._append_event(db, run_id, "execution.claimed", {
                    "attempt_id": str(attempt_id), "fencing_token": token,
                }, now)
                await self._commit(db)
            except Exception:
                await db.rollback()
                raise
        return LeaseHandle(run_id=run_id, attempt_id=attempt_id, owner_id=owner_id,
                           fencing_token=token, lease_expires_at=expires)

    async def heartbeat(self, lease: LeaseHandle, *, lease_seconds: int) -> LeaseHandle:
        now = self._aware_now()
        expires = now + timedelta(seconds=lease_seconds)
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._validate_lease(db, lease, now)
                await db.execute(
                    "UPDATE execution_attempts SET lease_expires_at=?, heartbeat_at=? WHERE id=?",
                    (_dump_time(expires), _dump_time(now), str(lease.attempt_id)),
                )
                await self._commit(db)
            except Exception:
                await db.rollback()
                raise
        return lease.model_copy(update={"lease_expires_at": expires})

    async def release(self, lease: LeaseHandle) -> AttemptRecord:
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._validate_lease(db, lease, now)
                await db.execute(
                    "UPDATE execution_attempts SET status='released', finished_at=? WHERE id=?",
                    (_dump_time(now), str(lease.attempt_id)),
                )
                await self._settle_assistant_time(db, lease.run_id, now)
                await db.execute(
                    "UPDATE execution_run_heads SET active_attempt_id=NULL WHERE run_id=?",
                    (str(lease.run_id),),
                )
                await self._set_run_status(db, lease.run_id, "waiting", now)
                await self._commit(db)
            except Exception:
                await db.rollback()
                raise
        return await self.get_attempt(lease.attempt_id)

    async def commit_checkpoint(self, lease: LeaseHandle, state: dict[str, Any]) -> None:
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._validate_lease(db, lease, now)
                cursor = await db.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_run_checkpoints WHERE run_id=?",
                    (str(lease.run_id),),
                )
                sequence = (await cursor.fetchone())[0]
                await db.execute(
                    "INSERT INTO task_run_checkpoints(run_id, sequence, state, created_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET sequence=excluded.sequence, "
                    "state=excluded.state, created_at=excluded.created_at",
                    (str(lease.run_id), sequence, json.dumps(state), _dump_time(now)),
                )
                await self._commit(db)
            except Exception:
                await db.rollback()
                raise

    async def request_cancel(self, run_id: UUID, *, expected_version=None, request=None) -> RunRecord:
        now = self._aware_now()
        digest = canonical_digest(request.model_dump(mode="json")) if request is not None else None
        if request is not None:
            expected_version = request.expected_version
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            try:
                await db.execute("BEGIN IMMEDIATE")
                run = await self._run_row(db, run_id)
                if run is None:
                    raise KeyError(str(run_id))
                if request is not None:
                    existing = await (await db.execute(
                        "SELECT digest FROM task_actions WHERE run_id=? AND client_request_id=?",
                        (str(run_id), request.client_request_id))).fetchone()
                    if existing:
                        if existing[0] != digest:
                            raise RunRepositoryConflict("action_request_conflict")
                        await db.rollback()
                        return _run_record(run)
                if expected_version is not None and run[6] != expected_version:
                    raise RunRepositoryConflict("version_conflict")
                if request is not None:
                    await db.execute("INSERT INTO task_actions(run_id,client_request_id,digest) VALUES (?,?,?)",
                                     (str(run_id), request.client_request_id, digest))
                if run[4] in {"completed", "failed", "cancelled", "timed_out"}:
                    await self._commit(db)
                    return _run_record(run)
                await db.execute(
                    "UPDATE execution_run_heads SET cancel_requested=1, active_attempt_id=NULL WHERE run_id=?",
                    (str(run_id),),
                )
                await db.execute(
                    "UPDATE execution_attempts SET status='cancelled', finished_at=? "
                    "WHERE run_id=? AND status='running'",
                    (_dump_time(now), str(run_id)),
                )
                await self._set_run_status(db, run_id, "cancelled", now)
                await self._append_event(db, run_id, "execution.cancelled", {}, now)
                await self._commit(db)
            except Exception:
                await db.rollback()
                raise
        return await self.get_run(run_id)

    async def fail_unclaimed(self, run_id: UUID, code: str) -> RunRecord:
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            try:
                await db.execute("BEGIN IMMEDIATE")
                run = await self._run_row(db, run_id)
                if run is None:
                    raise KeyError(str(run_id))
                if run[4] in {"completed", "failed", "cancelled", "timed_out"}:
                    await db.rollback()
                    return _run_record(run)
                head = await self._head_row(db, run_id)
                if head is not None:
                    raise RuntimeError("cannot fail a managed run without its execution owner")
                await self._set_run_status(db, run_id, "failed", now)
                await db.execute("UPDATE task_runs SET error=? WHERE id=?",
                                 (json.dumps({"code": code}), str(run_id)))
                await self._append_event(db, run_id, "execution.failed", {"code": code}, now)
                await self._commit(db)
            except Exception:
                await db.rollback()
                raise
        return await self.get_run(run_id)

    async def finish(self, lease: LeaseHandle, status: str = "completed", *, snapshot=None,
                     event_type: str | None = None, event_payload: dict[str, Any] | None = None,
                      error: dict[str, Any] | None = None, result=None, stage=None,
                      pending_action=None, domain_run_id=None) -> RunRecord:
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._validate_lease(db, lease, now)
                row = await self._run_row(db, lease.run_id)
                await self._settle_assistant_time(db, lease.run_id, now)
                if row[4] == "cancelled":
                    raise StaleExecutionOwner("run has been cancelled")
                await self._set_run_status(db, lease.run_id, status, now)
                await db.execute("UPDATE task_runs SET error=? WHERE id=?",
                                 (json.dumps(error) if error else None, str(lease.run_id)))
                if row[1] in {"chat", "knowledge", "writing", "assistant"}:
                    await db.execute(
                        "UPDATE task_runs SET result=?, stage=?, pending_action=?, domain_run_id=? WHERE id=?",
                        (json.dumps(result) if result is not None else None, stage,
                         pending_action.model_dump_json() if pending_action else None,
                         domain_run_id or row[11], str(lease.run_id)))
                if snapshot is not None:
                    if snapshot.run_id != lease.run_id:
                        raise RecoveryConflict("snapshot_run_mismatch")
                    if snapshot.snapshot_type == "assistant":
                        current = await (await db.execute("SELECT state FROM task_run_checkpoints WHERE run_id=?",
                            (str(lease.run_id),))).fetchone()
                        raw = json.loads(current[0])
                        snapshot = snapshot.model_copy(update={
                            "reserved_model_calls": raw["reserved_model_calls"],
                            "elapsed_upper_bound_seconds": raw["elapsed_upper_bound_seconds"]})
                    cursor = await db.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_run_checkpoints WHERE run_id=?",
                        (str(lease.run_id),))
                    checkpoint_sequence = (await cursor.fetchone())[0]
                    event_cursor = await db.execute(
                        "SELECT COALESCE(MAX(sequence), 0) FROM task_run_events WHERE run_id=?",
                        (str(lease.run_id),))
                    event_sequence = (await event_cursor.fetchone())[0]
                    saved = snapshot.model_copy(update={
                        "checkpoint_revision": snapshot.checkpoint_revision + 1,
                        "last_event_sequence": event_sequence + (1 if event_type else 0),
                    })
                    await db.execute(
                        "INSERT INTO task_run_checkpoints(run_id, sequence, state, created_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(run_id) DO UPDATE SET sequence=excluded.sequence, state=excluded.state, created_at=excluded.created_at",
                        (str(lease.run_id), checkpoint_sequence, saved.model_dump_json(), _dump_time(now)))
                if event_type:
                    payload = dict(event_payload or {})
                    payload.update({"fencing_token": lease.fencing_token, "attempt_id": str(lease.attempt_id)})
                    await self._append_event(db, lease.run_id, event_type, payload, now)
                await db.execute(
                    "UPDATE execution_attempts SET status=?, finished_at=? WHERE id=?",
                    ("succeeded" if status == "completed" else "released" if status == "waiting" else "cancelled" if status == "cancelled" else "failed",
                     _dump_time(now), str(lease.attempt_id)),
                )
                await db.execute(
                    "UPDATE execution_run_heads SET active_attempt_id=NULL WHERE run_id=?",
                    (str(lease.run_id),),
                )
                await self._commit(db)
            except Exception:
                await db.rollback()
                raise
        return await self.get_run(lease.run_id)

    async def latest_attempt(self, run_id: UUID):
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute(
                "SELECT id, run_id, attempt_no, owner_id, fencing_token, status, lease_expires_at, "
                "heartbeat_at, started_at, finished_at, error_code FROM execution_attempts "
                "WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1", (str(run_id),))).fetchone()
        if row is None:
            raise KeyError(str(run_id))
        return _attempt_record(row)

    async def get_run(self, run_id: UUID) -> RunRecord | None:
        async with aiosqlite.connect(self.path) as db:
            row = await self._run_row(db, run_id)
        if row is None:
            return None
        return _run_record(row)

    async def get_snapshot(self, run_id: UUID):
        from .models import parse_execution_snapshot
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute("SELECT state FROM task_run_checkpoints WHERE run_id=?",
                                          (str(run_id),))).fetchone()
        return parse_execution_snapshot(row[0]) if row else None

    async def resume(self, run_id: UUID, *, expected_version: int, confirmed: bool) -> RunRecord:
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            try:
                await db.execute("BEGIN IMMEDIATE")
                run = await self._run_row(db, run_id)
                if run is None:
                    raise KeyError(str(run_id))
                if run[6] != expected_version:
                    raise RunRepositoryConflict("Run version conflict")
                if run[4] != "waiting":
                    raise RecoveryConflict("run_not_waiting")
                head = await self._head_row(db, run_id)
                if head and head[1] is not None:
                    raise RecoveryConflict("active_execution_owner")
                unresolved = await (await db.execute(
                    "SELECT 1 FROM execution_operations WHERE run_id=? AND status IN ('running','unknown')",
                    (str(run_id),),
                )).fetchone()
                if unresolved:
                    raise RecoveryConflict("manual_recovery_required")
                await self._set_run_status(db, run_id, "queued", now, resume_confirmed=confirmed)
                await self._append_event(db, run_id, "execution.resumed", {"schema_version": 1}, now)
                await self._commit(db)
                return _run_record(await self._run_row(db, run_id))
            except Exception:
                await db.rollback()
                raise

    async def bootstrap_snapshot(self, snapshot) -> None:
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            await db.execute("BEGIN IMMEDIATE")
            exists = await (await db.execute("SELECT 1 FROM task_runs WHERE id=?", (str(snapshot.run_id),))).fetchone()
            if exists is None:
                raise KeyError(str(snapshot.run_id))
            await db.execute("INSERT INTO task_run_checkpoints(run_id, sequence, state, created_at) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT(run_id) DO NOTHING",
                             (str(snapshot.run_id), 1, snapshot.model_dump_json(), _dump_time(now)))
            await self._commit(db)

    async def bootstrap_run(self, snapshot, *, event_type: str, event_payload: dict[str, Any]) -> None:
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            try:
                await db.execute("BEGIN IMMEDIATE")
                exists = await (await db.execute(
                    "SELECT 1 FROM task_runs WHERE id=?", (str(snapshot.run_id),)
                )).fetchone()
                if exists is None:
                    raise KeyError(str(snapshot.run_id))
                await db.execute(
                    "INSERT INTO task_run_checkpoints(run_id, sequence, state, created_at) "
                    "VALUES (?, 1, ?, ?) ON CONFLICT(run_id) DO NOTHING",
                    (str(snapshot.run_id), snapshot.model_dump_json(), _dump_time(now)),
                )
                await self._append_event(db, snapshot.run_id, event_type, event_payload, now)
                await self._commit(db)
            except Exception:
                await db.rollback()
                raise

    async def save_snapshot(self, lease: LeaseHandle, snapshot: ExecutionSnapshot):
        return await self._operation_transaction(lease, snapshot)

    async def commit_assistant_message(self, lease, snapshot, *, text, expected_logical_index):
        """Commit the visible message and its next checkpoint in one fenced transaction."""
        if (snapshot.snapshot_type != "assistant" or snapshot.phase != "ready_model"
                or snapshot.logical_index != expected_logical_index + 1):
            raise RecoveryConflict("invalid_message_transition")
        return await self._operation_transaction(lease, snapshot,
            assistant_message=(expected_logical_index, text))

    async def get_model_calls(self, run_id):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM execution_model_calls WHERE run_id=? ORDER BY ordinal",
                (str(run_id),))).fetchall()
        return [dict(row) for row in rows]

    async def _settle_assistant_time(self, db, run_id, now, *, minimum=0.0):
        row = await (await db.execute("SELECT state,created_at FROM task_run_checkpoints WHERE run_id=?",
                                     (str(run_id),))).fetchone()
        if row is None:
            return
        state = json.loads(row[0])
        if state.get("snapshot_type") != "assistant":
            return
        delta = max(minimum, 0.0, (now - _parse_time(row[1])).total_seconds())
        state["elapsed_upper_bound_seconds"] += delta
        state["checkpoint_revision"] += 1
        await db.execute("UPDATE task_run_checkpoints SET state=?,created_at=?,sequence=sequence+1 WHERE run_id=?",
                         (json.dumps(state), _dump_time(now), str(run_id)))

    async def reserve_model_call(self, lease, *, scope, step_id, operation_id, phase,
                                 attempt_id, input_digest, config_digest, timeout_seconds):
        from .models import AssistantExecutionSnapshot, parse_execution_snapshot
        if scope not in {"top", "domain"} or not phase or not attempt_id:
            raise ValueError("invalid model call scope")
        if scope == "top" and not step_id or scope == "domain" and not operation_id:
            raise ValueError("model call requires stable step or parent operation")
        if timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            db.row_factory = aiosqlite.Row
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._validate_lease(db, lease, now)
                await self._settle_assistant_time(db, lease.run_id, now)
                row = await (await db.execute("SELECT state FROM task_run_checkpoints WHERE run_id=?",
                                             (str(lease.run_id),))).fetchone()
                snap = parse_execution_snapshot(row[0]) if row else None
                if not isinstance(snap, AssistantExecutionSnapshot):
                    raise RecoveryConflict("assistant_snapshot_required")
                if snap.config_digest != config_digest:
                    raise RecoveryConflict("model_config_conflict")
                if scope == "top":
                    old = await (await db.execute("SELECT * FROM execution_model_calls "
                        "WHERE run_id=? AND scope='top' AND step_id=? ORDER BY ordinal DESC LIMIT 1",
                        (str(lease.run_id), step_id))).fetchone()
                    if old:
                        if old["input_digest"] != input_digest or old["config_digest"] != config_digest:
                            raise RecoveryConflict("model_input_conflict")
                        if old["status"] == "committed":
                            await db.rollback()
                            return dict(old)
                        if old["status"] == "reserved":
                            raise RecoveryConflict("model_call_in_flight")
                if await (await db.execute("SELECT 1 FROM execution_operations WHERE run_id=? "
                    "AND status='running' AND recovery_class NOT IN ('model','read_only') LIMIT 1",
                    (str(lease.run_id),))).fetchone():
                    raise RecoveryConflict("unknown_side_effect")
                await self._assert_assistant_effect_allowed(db, lease.run_id, for_model=True)
                count, inflight = await (await db.execute("SELECT COUNT(*), "
                    "COALESCE(SUM(CASE WHEN status='reserved' THEN timeout_seconds ELSE 0 END),0) "
                    "FROM execution_model_calls WHERE run_id=?", (str(lease.run_id),))).fetchone()
                if count >= snap.budget_limits["max_model_calls"]:
                    raise RuntimeError("model_budget_exhausted")
                remaining = snap.budget_limits["max_execution_seconds"] - snap.elapsed_upper_bound_seconds - inflight
                if remaining <= 0:
                    raise TimeoutError("run_time_exhausted")
                timeout = min(float(timeout_seconds), remaining)
                call_id = str(uuid4())
                await db.execute("INSERT INTO execution_model_calls "
                    "(id,run_id,lease_attempt_id,scope,step_id,operation_id,phase,attempt_id,ordinal,"
                    "input_digest,config_digest,status,timeout_seconds,started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (call_id, str(lease.run_id), str(lease.attempt_id), scope, step_id, operation_id,
                     phase, attempt_id, count + 1, input_digest, config_digest, "reserved", timeout, _dump_time(now)))
                snap = snap.model_copy(update={"reserved_model_calls": count + 1,
                    "checkpoint_revision": snap.checkpoint_revision + 1})
                await db.execute("UPDATE task_run_checkpoints SET sequence=sequence+1,state=?,created_at=? WHERE run_id=?",
                                 (snap.model_dump_json(), _dump_time(now), str(lease.run_id)))
                await self._append_event(db, lease.run_id, "assistant.model_reserved",
                    {"call_id": call_id, "ordinal": count + 1, "scope": scope}, now)
                result = dict(await (await db.execute("SELECT * FROM execution_model_calls WHERE id=?", (call_id,))).fetchone())
                await self._commit(db)
                return result
            except BaseException:
                await db.rollback()
                raise

    async def commit_model_call(self, lease, call_id, response=None, *, error_code=None):
        from .models import parse_execution_snapshot
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            db.row_factory = aiosqlite.Row
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._validate_lease(db, lease, now)
                row = await (await db.execute("SELECT * FROM execution_model_calls WHERE id=? AND run_id=?",
                    (str(call_id), str(lease.run_id)))).fetchone()
                if not row or row["lease_attempt_id"] != str(lease.attempt_id) or row["status"] != "reserved":
                    raise RecoveryConflict("model_call_not_reserved")
                await self._settle_assistant_time(db, lease.run_id, now,
                    minimum=row["timeout_seconds"] if error_code in {"timeout", "cancelled"} else 0.0)
                raw = await (await db.execute("SELECT state FROM task_run_checkpoints WHERE run_id=?", (str(lease.run_id),))).fetchone()
                snap = parse_execution_snapshot(raw[0])
                snap = snap.model_copy(update={
                    "checkpoint_revision": snap.checkpoint_revision + 1})
                await db.execute("UPDATE execution_model_calls SET status=?,response=?,error_code=?,finished_at=? WHERE id=?",
                    ("committed" if response is not None else "failed", response.model_dump_json() if response is not None else None,
                     error_code, _dump_time(now), str(call_id)))
                await db.execute("UPDATE task_run_checkpoints SET sequence=sequence+1,state=?,created_at=? WHERE run_id=?",
                                 (snap.model_dump_json(), _dump_time(now), str(lease.run_id)))
                await self._append_event(db, lease.run_id, "assistant.model_settled", {"call_id": str(call_id),
                    "status": "committed" if response is not None else "failed"}, now)
                await self._commit(db)
            except BaseException:
                await db.rollback()
                raise

    async def link_stage(self, lease, snapshot):
        """Persist domain association and stage checkpoint under the same fence."""
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            await db.execute("BEGIN IMMEDIATE")
            await self._validate_lease(db, lease, now)
            if snapshot.run_id != lease.run_id:
                raise RecoveryConflict("snapshot_run_mismatch")
            await db.execute("UPDATE task_runs SET domain_run_id=?,stage=? WHERE id=?",
                             (snapshot.domain_run_id, snapshot.stage, str(lease.run_id)))
            await db.execute(
                "UPDATE task_run_checkpoints SET sequence=sequence+1,state=?,created_at=? WHERE run_id=?",
                (snapshot.model_dump_json(), _dump_time(now), str(lease.run_id)))
            await self._commit(db)

    async def queue_assistant_action(self, run_id, request, *, confirmation_validator=None, workspace_validator=None, recovery_validator=None):
        """Consume a frozen assistant action atomically, without creating another Run."""
        from .models import AssistantExecutionSnapshot, parse_execution_snapshot
        from app.agent.models import Message
        now = self._aware_now()
        digest = canonical_digest(request.model_dump(mode="json"))
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await self._run_row(db, run_id)
            existing = await (await db.execute(
                "SELECT digest FROM task_actions WHERE run_id=? AND client_request_id=?",
                (str(run_id), request.client_request_id))).fetchone()
            if existing:
                raise RunRepositoryConflict("stale_action")
            head = await self._head_row(db, run_id)
            if row[1] != "assistant" or row[4] != "waiting" or row[6] != request.expected_version or (head and head[1]):
                raise RunRepositoryConflict("stale_action")
            raw = await (await db.execute("SELECT state FROM task_run_checkpoints WHERE run_id=?",
                                         (str(run_id),))).fetchone()
            snapshot = parse_execution_snapshot(raw[0])
            if not isinstance(snapshot, AssistantExecutionSnapshot):
                raise RunRepositoryConflict("snapshot_type_conflict")
            pending = snapshot.pending_action
            if not pending or pending.action_id != request.action_id or request.action not in pending.allowed_actions:
                raise RunRepositoryConflict("action_not_allowed")
            if request.action == "resolve_side_effect":
                return await self._resolve_assistant_side_effect(db, row, snapshot, pending, request, digest, now, recovery_validator)
            if request.action not in {"provide_input", "reject_action", "approve_action", "bind_workspace"} or pending.action_type == "recovery":
                raise RunRepositoryConflict("action_not_allowed")
            if request.action == "provide_input":
                if pending.action_type != "clarification":
                    raise RunRepositoryConflict("action_not_allowed")
                answer = request.payload
                if (set(answer) == {"choice"} and answer["choice"] not in pending.frozen_parameters.get("choices", [])
                        or set(answer) == {"message"} and not pending.frozen_parameters.get("allow_free_text", True)):
                    raise ValueError("invalid_answer")
            workspace_id = snapshot.workspace_id
            if request.action == "bind_workspace":
                if pending.action_type != "workspace_binding" or workspace_id is not None or row[3] is not None:
                    raise RunRepositoryConflict("action_not_allowed")
                if workspace_validator is None:
                    raise RunRepositoryConflict("action_not_allowed")
                if await (await db.execute("SELECT 1 FROM execution_operations WHERE run_id=? AND "
                    "tool_name IN ('list_files','read_file','search_files','write_file','apply_patch','run_tests','get_diff','collect_artifact')",
                    (str(run_id),))).fetchone():
                    raise RunRepositoryConflict("project_operation_exists")
                workspace_id = request.payload["workspace_id"]
                workspace_validator(workspace_id)
                if not await (await db.execute("SELECT 1 FROM sessions WHERE id=?", (row[9],))).fetchone():
                    raise RunRepositoryConflict("session_not_found")
                if snapshot.session_id != UUID(row[9]):
                    raise RunRepositoryConflict("session_mismatch")
                if await (await db.execute("SELECT 1 FROM task_runs WHERE workspace_id=? AND id!=? "
                    "AND status IN ('queued','running','waiting')", (workspace_id, str(run_id)))).fetchone():
                    raise RunRepositoryConflict("workspace_busy")
                if await (await db.execute("SELECT 1 FROM execution_operations o JOIN task_runs r ON r.id=o.run_id "
                    "WHERE r.workspace_id=? AND o.recovery_class IN ('file_write','sandbox','artifact') "
                    "AND (o.status='running' OR (o.status='unknown' AND NOT EXISTS "
                    "(SELECT 1 FROM assistant_effect_settlements s WHERE s.invocation_id=o.id "
                    "AND s.quarantined=0 AND s.conflict=0 AND s.termination_evidence IS NOT NULL)))", (workspace_id,))).fetchone():
                    raise RunRepositoryConflict("workspace_busy_unknown_outcome")
                await db.execute("UPDATE task_runs SET workspace_id=? WHERE id=?", (workspace_id, str(run_id)))
            if request.action == "approve_action":
                if pending.action_type != "confirmation" or confirmation_validator is None:
                    raise RunRepositoryConflict("confirmation_handler_unavailable")
                confirmation_validator(pending.model_copy(deep=True), dict(request.payload))
            state = dict(snapshot.harness_state)
            dynamic = dict(state.get("structured_state", {}))
            dynamic["workspace_id"] = workspace_id
            dynamic["action_feedback"] = {"action_id": pending.action_id, "action": request.action,
                "payload": request.payload, "target_capability": pending.target_capability,
                "frozen_parameters": pending.frozen_parameters, "business_version": pending.business_version}
            if request.action == "reject_action":
                if pending.action_type == "workspace_binding":
                    dynamic["workspace_rejected"] = True
                dynamic["rejected_actions"] = [*dynamic.get("rejected_actions", []), pending.action_id]
                if pending.target_capability:
                    dynamic["blocked_capabilities"] = {**dynamic.get("blocked_capabilities", {}),
                        pending.target_capability: "user_rejected"}
            state["structured_state"] = dynamic
            messages = [*snapshot.messages, Message(role="user", content=json.dumps(
                {"action": request.action, "payload": request.payload}, ensure_ascii=False))]
            saved = snapshot.model_copy(update={"phase": "ready_model", "pending_action": None,
                "pending_decision": None, "pending_response": None, "harness_state": state,
                "messages": messages, "workspace_id": workspace_id,
                "checkpoint_revision": snapshot.checkpoint_revision + 1})
            await self._set_run_status(db, run_id, "queued", now, resume_confirmed=True)
            await db.execute("UPDATE task_runs SET pending_action=NULL WHERE id=?", (str(run_id),))
            await db.execute("UPDATE task_run_checkpoints SET sequence=sequence+1,state=?,created_at=? WHERE run_id=?",
                             (saved.model_dump_json(), _dump_time(now), str(run_id)))
            await db.execute("INSERT INTO task_actions(run_id,client_request_id,digest) VALUES (?,?,?)",
                             (str(run_id), request.client_request_id, digest))
            await self._append_event(db, run_id, "assistant.action", {"schema_version": 1,
                "action_id": pending.action_id, "action": request.action}, now)
            await self._assistant_recovery_event(db, run_id, "assistant.resumed", now,
                {"action_id": pending.action_id, "action": request.action})
            await self._commit(db)
            return _run_record(await self._run_row(db, run_id))

    async def _assistant_recovery_event(self, db, run_id, event_type, now, data, invocation_id=None):
        from app.assistant.models import RuntimeEvent, RuntimeEventDetail
        summaries = {"assistant.resumed": "用户动作已提交，原 Run 等待继续",
            "tool.reconciled": "用户已提交副作用核查事实", "tool.late_result": "已取消操作的迟到结果已归档",
            "assistant.interrupt_requested": "当前任务调整请求已接收",
            "assistant.safe_point_reached": "当前执行已到达安全点",
            "assistant.input_revision_added": "当前任务输入修订已保存",
            "assistant.steer_applied": "当前任务调整已生效",
            "tool.settled": "核查结论已结算，隔离权限单独判定",
            "tool.quarantine_released": "服务端已验证旧执行终止并解除隔离",
            "tool.abandoned": "旧方向操作在启动前已放弃"}
        event = RuntimeEvent(event_id=uuid4(), run_id=UUID(str(run_id)), invocation_id=str(invocation_id) if invocation_id else None,
            event_type=event_type, occurred_at=now, summary=summaries[event_type],
            detail=RuntimeEventDetail(data=data))
        await self._append_event(db, run_id, event_type, event.model_dump(mode="json"), now, event_id=event.event_id)

    async def _resolve_assistant_side_effect(self, db, row, snapshot, pending, request, digest, now, recovery_validator):
        """Append user observations; preserve the original unknown operation and Step."""
        from app.agent.models import Message
        resolution = request.payload["resolution"]
        invocation_id = pending.frozen_parameters.get("invocation_id")
        op = await (await db.execute("SELECT status,tool_name FROM execution_operations WHERE id=? AND run_id=?",
            (invocation_id, str(snapshot.run_id)))).fetchone()
        if (pending.action_type != "recovery" or invocation_id != str(snapshot.pending_operation_id)
                or not op or op[0] != "unknown"):
            raise RunRepositoryConflict("action_not_allowed")
        await self._assistant_recovery_event(db, snapshot.run_id, "tool.reconciled", now,
            {"resolution": resolution, "observation_scope": "user_reported", "action_id": pending.action_id}, invocation_id)
        await db.execute("INSERT INTO assistant_effect_settlements(invocation_id,resolution) VALUES (?,?) "
            "ON CONFLICT(invocation_id) DO UPDATE SET resolution=excluded.resolution,quarantined=1",
            (invocation_id, resolution))
        if resolution != "unknown":
            await self._assistant_recovery_event(db, snapshot.run_id, "tool.settled", now,
                {"resolution": resolution, "quarantined": True, "basis": "user_reported"}, invocation_id)
        state = dict(snapshot.harness_state)
        dynamic = dict(state.get("structured_state", {}))
        dynamic["side_effect_resolutions"] = {**dynamic.get("side_effect_resolutions", {}), invocation_id: resolution}
        dynamic["action_feedback"] = {"action": request.action, "payload": request.payload, "invocation_id": invocation_id}
        state["structured_state"] = dynamic
        waiting = resolution == "unknown"
        retry = (resolution == "not_executed" and recovery_validator is not None
            and recovery_validator(pending.model_copy(deep=True), op[1]) is True)
        if retry:
            dynamic["retry_invocation_id"] = invocation_id
        next_action = pending.model_copy(update={"action_id": str(uuid4())}) if waiting else None
        saved = snapshot.model_copy(update={"phase": "waiting" if waiting else "pending_tool" if retry else "ready_model",
            "pending_action": next_action, "pending_operation_id": snapshot.pending_operation_id if waiting or retry else None,
            "logical_index": snapshot.logical_index + (0 if waiting or retry else 1), "harness_state": state,
            "pending_response": None, "pending_decision": None,
            "messages": [*snapshot.messages, Message(role="user", content=json.dumps({
                "side_effect_verification": {"invocation_id": invocation_id, "resolution": resolution,
                    "retry_allowed": retry}}, ensure_ascii=False))],
            "checkpoint_revision": snapshot.checkpoint_revision + 1})
        if not waiting:
            await self._set_run_status(db, snapshot.run_id, "queued", now, resume_confirmed=True)
            await self._assistant_recovery_event(db, snapshot.run_id, "assistant.resumed", now,
                {"action_id": pending.action_id, "action": request.action}, invocation_id)
        else:
            await db.execute("UPDATE task_runs SET version=version+1,updated_at=? WHERE id=?", (_dump_time(now), str(snapshot.run_id)))
        await db.execute("UPDATE task_runs SET pending_action=? WHERE id=?",
            (next_action.model_dump_json() if next_action else None, str(snapshot.run_id)))
        await db.execute("UPDATE task_run_checkpoints SET sequence=sequence+1,state=?,created_at=? WHERE run_id=?",
            (saved.model_dump_json(), _dump_time(now), str(snapshot.run_id)))
        await db.execute("INSERT INTO task_actions(run_id,client_request_id,digest) VALUES (?,?,?)",
            (str(snapshot.run_id), request.client_request_id, digest))
        await self._commit(db)
        return _run_record(await self._run_row(db, snapshot.run_id))

    async def assert_assistant_effect_allowed(self, run_id):
        async with aiosqlite.connect(self.path) as db:
            await self._assert_assistant_effect_allowed(db, run_id)

    async def _assert_assistant_effect_allowed(self, db, run_id, *, for_model=False):
        row = await (await db.execute("SELECT state FROM task_run_checkpoints WHERE run_id=?", (str(run_id),))).fetchone()
        state = json.loads(row[0]) if row else {}
        resolved = state.get("harness_state", {}).get("structured_state", {}).get("side_effect_resolutions", {})
        operations = await (await db.execute("SELECT id FROM execution_operations WHERE run_id=? "
            "AND recovery_class != 'read_only' AND status='unknown'", (str(run_id),))).fetchall()
        if any(resolved.get(op[0]) not in {"executed", "not_executed"} for op in operations):
            raise RecoveryConflict("unknown_side_effect")
        if not for_model:
            retry = state.get("harness_state", {}).get("structured_state", {}).get("retry_invocation_id")
            for op in operations:
                if op[0] == retry:
                    continue
                settled = await (await db.execute("SELECT quarantined,conflict FROM assistant_effect_settlements "
                    "WHERE invocation_id=?", (op[0],))).fetchone()
                if not settled or settled[0] or settled[1]:
                    raise RecoveryConflict("unknown_side_effect")

    async def record_assistant_late_result(self, lease, step_id, outcome):
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            await db.execute("BEGIN IMMEDIATE")
            run = await self._run_row(db, lease.run_id)
            step = await (await db.execute("SELECT s.operation_id FROM execution_steps s JOIN execution_operations o "
                "ON o.id=s.operation_id WHERE s.id=? AND s.attempt_id=? AND o.run_id=?",
                (str(step_id), str(lease.attempt_id), str(lease.run_id)))).fetchone()
            if not step or run[1] != "assistant" or run[4] != "cancelled":
                raise StaleExecutionOwner("late_result_not_allowed")
            await self._assistant_recovery_event(db, lease.run_id, "tool.late_result", now,
                {"step_id": str(step_id), "success": outcome.success,
                    "result_digest": canonical_digest(outcome.result_payload), "observation_scope": "runtime_local"}, step[0])
            # A returned local call is not proof that a remote/background writer has stopped.
            # Preserve quarantine and flag contradictions independently of the user's report.
            await db.execute("UPDATE assistant_effect_settlements SET conflict=1,quarantined=1 "
                "WHERE invocation_id=? AND resolution='not_executed' AND ?=1", (step[0], int(outcome.success)))
            await self._commit(db)

    async def queue_stage_action(self, run_id, request):
        from .models import parse_execution_snapshot
        now = self._aware_now()
        digest = canonical_digest(request.model_dump(mode="json"))
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await self._run_row(db, run_id)
            existing = await (await db.execute(
                "SELECT digest FROM task_actions WHERE run_id=? AND client_request_id=?",
                (str(run_id), request.client_request_id))).fetchone()
            if existing:
                if existing[0] != digest:
                    raise RunRepositoryConflict("action_request_conflict")
                return _run_record(row)
            head = await self._head_row(db, run_id)
            if row[4] != "waiting" or row[6] != request.expected_version or (head and head[1]):
                raise RunRepositoryConflict("stale_action")
            raw = await (await db.execute("SELECT state FROM task_run_checkpoints WHERE run_id=?",
                                         (str(run_id),))).fetchone()
            snapshot = parse_execution_snapshot(raw[0])
            pending = snapshot.pending_action
            if not pending or pending.action_id != request.action_id or request.action not in pending.allowed_actions:
                raise RunRepositoryConflict("action_not_allowed")
            stage = "confirm_outline" if request.action == "confirm_outline" else "saving"
            saved = snapshot.model_copy(update={"phase": "ready", "stage": stage,
                "action_payload": request.payload, "save_key": snapshot.save_key or f"save-{run_id}",
                "checkpoint_revision": snapshot.checkpoint_revision + 1})
            await self._set_run_status(db, run_id, "queued", now, resume_confirmed=True)
            await db.execute("UPDATE task_runs SET stage=?,pending_action=NULL WHERE id=?", (stage, str(run_id)))
            await db.execute("UPDATE task_run_checkpoints SET sequence=sequence+1,state=?,created_at=? WHERE run_id=?",
                             (saved.model_dump_json(), _dump_time(now), str(run_id)))
            await db.execute("INSERT INTO task_actions(run_id,client_request_id,digest) VALUES (?,?,?)",
                             (str(run_id), request.client_request_id, digest))
            await self._append_event(db, run_id, "task.action", {"schema_version": 1, "action": request.action}, now)
            await self._commit(db)
            return _run_record(await self._run_row(db, run_id))

    async def get_stage_action_replay(self, run_id, request):
        async with aiosqlite.connect(self.path) as db:
            existing = await (await db.execute(
                "SELECT digest FROM task_actions WHERE run_id=? AND client_request_id=?",
                (str(run_id), request.client_request_id))).fetchone()
        if not existing:
            return None
        if existing[0] != canonical_digest(request.model_dump(mode="json")):
            raise RunRepositoryConflict("action_request_conflict")
        return await self.get_run(run_id)

    async def recover_stage(self, candidate, *, status, result=None, pending_action=None, error=None):
        from .models import parse_execution_snapshot
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            await db.execute("BEGIN IMMEDIATE")
            current = await self._candidate(db, candidate.run_id)
            head = await self._head_row(db, candidate.run_id)
            if current != candidate or head[1] is not None:
                raise RecoveryConflict("recovery_candidate_changed")
            await self._set_run_status(db, candidate.run_id, status, now)
            await db.execute("UPDATE task_runs SET result=?,pending_action=?,error=? WHERE id=?",
                (json.dumps(result) if result is not None else None,
                 pending_action.model_dump_json() if pending_action else None,
                 json.dumps(error) if error else None, str(candidate.run_id)))
            snapshot = parse_execution_snapshot(json.dumps(candidate.snapshot))
            snapshot = snapshot.model_copy(update={"pending_action": pending_action,
                "phase": "waiting" if status == "waiting" else "finished",
                "checkpoint_revision": snapshot.checkpoint_revision + 1})
            await db.execute("UPDATE task_run_checkpoints SET sequence=sequence+1,state=?,created_at=? WHERE run_id=?",
                             (snapshot.model_dump_json(), _dump_time(now), str(candidate.run_id)))
            await self._append_event(db, candidate.run_id, "task.recovered",
                {"schema_version": 1, "status": status, "code": (error or {}).get("code")}, now)
            await self._commit(db)
        return await self.get_run(candidate.run_id)

    async def prepare_operation(self, lease, operation, snapshot):
        await self._operation_transaction(lease, snapshot, operation=operation)
        return await self.get_operation(operation.id)

    async def commit_model_response(self, lease, response, snapshot, operation=None):
        snapshot = snapshot.model_copy(update={"pending_response": response})
        return await self._operation_transaction(lease, snapshot, operation=operation,
                                                 event_type="model.succeeded")

    async def start_step(self, lease, operation_id, snapshot):
        step_id = uuid4()
        await self._operation_transaction(lease, snapshot, start=(step_id, operation_id))
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute(
                "SELECT execution_no FROM execution_steps WHERE id=?", (str(step_id),))).fetchone()
        return StepRecord(id=step_id, operation_id=operation_id, attempt_id=lease.attempt_id,
                          execution_no=row[0], status="running")

    async def commit_step(self, lease, step_id, result, snapshot):
        return await self._operation_transaction(lease, snapshot, finish_step=(step_id, result))

    async def get_operation(self, operation_id):
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT * FROM execution_operations WHERE id=?", (str(operation_id),))
            row = await cursor.fetchone()
            if row is None:
                raise KeyError(str(operation_id))
            values = dict(zip([c[0] for c in cursor.description], row))
        values.pop("run_id")
        values.pop("result_ref")
        for key in ("input_payload", "result_payload", "before_digests", "after_digests"):
            values[key] = json.loads(values[key]) if values[key] else (None if key == "result_payload" else {})
        values["id"] = UUID(values["id"])
        return RecoveryOperation(**values)

    async def _operation_transaction(self, lease, snapshot, *, operation=None,
                                     start=None, finish_step=None, event_type=None, assistant_message=None):
        if snapshot.run_id != lease.run_id:
            raise RecoveryConflict("snapshot_run_mismatch")
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._validate_lease(db, lease, now)
                payload = {"attempt_id": str(lease.attempt_id), "fencing_token": lease.fencing_token}
                if snapshot.snapshot_type == "assistant":
                    current = await (await db.execute("SELECT state FROM task_run_checkpoints WHERE run_id=?",
                        (str(lease.run_id),))).fetchone()
                    if current:
                        raw = json.loads(current[0])
                        if assistant_message is not None and (
                                raw.get("snapshot_type") != "assistant"
                                or raw.get("logical_index") != assistant_message[0]
                                or raw.get("checkpoint_revision") != snapshot.checkpoint_revision
                                or raw.get("phase") not in {"ready_model", "ready_finish"}):
                            raise RecoveryConflict("message_checkpoint_conflict")
                        snapshot = snapshot.model_copy(update={
                            "reserved_model_calls": max(snapshot.reserved_model_calls, raw.get("reserved_model_calls", 0)),
                            "elapsed_upper_bound_seconds": max(snapshot.elapsed_upper_bound_seconds,
                                raw.get("elapsed_upper_bound_seconds", 0))})
                    elif assistant_message is not None:
                        raise RecoveryConflict("message_checkpoint_missing")
                if assistant_message is not None:
                    from app.assistant.models import RuntimeEvent, RuntimeEventDetail
                    index, text = assistant_message
                    event = RuntimeEvent(event_id=uuid4(), run_id=lease.run_id,
                        step_id=f"decision-{index}", event_type="assistant.message", occurred_at=now,
                        summary="阶段消息已提交",
                        detail=RuntimeEventDetail(data={"text": _assistant_audit_text(text)}))
                    await self._append_event(db, lease.run_id, event.event_type,
                        event.model_dump(mode="json"), now, event_id=event.event_id)
                if operation is not None:
                    digest = canonical_digest(operation.input_payload)
                    existing = await (await db.execute(
                        "SELECT id, input_digest FROM execution_operations WHERE run_id=? AND logical_index=?",
                        (str(lease.run_id), operation.logical_index))).fetchone()
                    existing_meta = None
                    if existing:
                        existing_meta = await (await db.execute(
                            "SELECT tool_name, recovery_class FROM execution_operations WHERE id=?", (existing[0],)
                        )).fetchone()
                    if existing and (existing[0] != str(operation.id) or existing[1] != digest
                                      or existing_meta != (operation.tool_name, operation.recovery_class)):
                        raise RecoveryConflict("operation_input_conflict")
                    await db.execute(
                        "INSERT OR IGNORE INTO execution_operations "
                        "(id, run_id, logical_index, kind, tool_name, input_digest, input_payload, recovery_class, "
                        "status, before_digests, after_digests) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)",
                        (str(operation.id), str(lease.run_id), operation.logical_index, operation.kind,
                         operation.tool_name, digest, json.dumps(operation.input_payload), operation.recovery_class,
                         json.dumps(operation.before_digests), json.dumps(operation.after_digests)))
                    payload["operation_id"] = str(operation.id)
                    await self._append_event(db, lease.run_id, "operation.prepared", payload, now)
                if start:
                    step_id, operation_id = start
                    if snapshot.snapshot_type == "assistant" and await (await db.execute(
                        "SELECT 1 FROM assistant_steer WHERE run_id=? AND status='requested'",
                        (str(lease.run_id),))).fetchone():
                        raise RecoveryConflict("steer_pending")
                    op = await (await db.execute(
                        "SELECT status,recovery_class FROM execution_operations WHERE id=? AND run_id=?",
                        (str(operation_id), str(lease.run_id)))).fetchone()
                    if op is None:
                        raise KeyError(str(operation_id))
                    retry = snapshot.snapshot_type == "assistant" and snapshot.harness_state.get(
                        "structured_state", {}).get("retry_invocation_id") == str(operation_id)
                    if op[0] != "prepared" and not (op[0] == "unknown" and retry):
                        raise RecoveryConflict("invalid_transition")
                    if snapshot.snapshot_type == "assistant" and op[1] != "read_only":
                        await self._assert_assistant_effect_allowed(db, lease.run_id)
                    number = await (await db.execute(
                        "SELECT COALESCE(MAX(execution_no),0)+1 FROM execution_steps WHERE operation_id=?",
                        (str(operation_id),))).fetchone()
                    await db.execute("INSERT INTO execution_steps "
                        "(id, operation_id, attempt_id, execution_no, status, started_at) VALUES (?, ?, ?, ?, 'running', ?)",
                        (str(step_id), str(operation_id), str(lease.attempt_id), number[0], _dump_time(now)))
                    await db.execute("UPDATE execution_operations SET status='running' WHERE id=?", (str(operation_id),))
                    payload.update(operation_id=str(operation_id), step_id=str(step_id))
                    event_type = "step.started"
                    if snapshot.snapshot_type == "assistant":
                        await self._append_assistant_tool_event(db, lease.run_id, operation_id,
                            step_id, "tool.started", now)
                if finish_step:
                    step_id, outcome = finish_step
                    row = await (await db.execute(
                        "SELECT s.operation_id, s.status, s.attempt_id FROM execution_steps s "
                        "JOIN execution_operations o ON o.id=s.operation_id WHERE s.id=? AND o.run_id=?",
                        (str(step_id), str(lease.run_id)))).fetchone()
                    if row is None:
                        raise KeyError(str(step_id))
                    if row[1] != "running" or row[2] != str(lease.attempt_id):
                        raise RecoveryConflict("invalid_transition")
                    status = "succeeded" if outcome.success else "failed"
                    if snapshot.snapshot_type == "assistant":
                        from app.assistant.models import CapabilityOutcome
                        observed = CapabilityOutcome.model_validate(outcome.result_payload["data"])
                        if observed.error and observed.error.category == "unknown_side_effect":
                            status = "unknown"
                    digest = canonical_digest(outcome.result_payload)
                    await db.execute("UPDATE execution_steps SET status=?, finished_at=?, output_digest=? WHERE id=?",
                                     (status, _dump_time(now), digest, str(step_id)))
                    await db.execute("UPDATE execution_operations SET status=?, result_payload=?, output_digest=? WHERE id=?",
                                     (status, json.dumps(outcome.result_payload), digest, row[0]))
                    payload.update(operation_id=row[0], step_id=str(step_id))
                    event_type = "step." + status
                    if snapshot.snapshot_type == "assistant":
                        tool_status = ("unknown" if observed.error and observed.error.category == "unknown_side_effect"
                            else "waiting" if observed.kind == "waiting"
                            else "succeeded" if observed.kind == "result" else "failed")
                        await self._append_assistant_tool_event(db, lease.run_id, row[0],
                            step_id, "tool." + tool_status, now, observed=observed)
                if event_type:
                    await self._append_event(db, lease.run_id, event_type, payload, now)
                row = await (await db.execute("SELECT state, sequence FROM task_run_checkpoints WHERE run_id=?",
                                             (str(lease.run_id),))).fetchone()
                revision = json.loads(row[0]).get("checkpoint_revision", 0) if row else 0
                event = await (await db.execute("SELECT COALESCE(MAX(sequence),0) FROM task_run_events WHERE run_id=?",
                                               (str(lease.run_id),))).fetchone()
                saved = snapshot.model_copy(update={"checkpoint_revision": revision + 1, "last_event_sequence": event[0]})
                await db.execute("INSERT INTO task_run_checkpoints(run_id, sequence, state, created_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(run_id) DO UPDATE SET sequence=excluded.sequence, state=excluded.state, created_at=excluded.created_at",
                    (str(lease.run_id), row[1] + 1 if row else 1, saved.model_dump_json(), _dump_time(now)))
                await self._commit(db)
                return saved
            except BaseException:
                await db.rollback()
                raise

    async def _append_assistant_tool_event(self, db, run_id, operation_id, step_id, event_type, now, *, observed=None):
        from app.assistant.models import RuntimeEvent, RuntimeEventDetail
        row = await (await db.execute("SELECT tool_name FROM execution_operations WHERE id=? AND run_id=?",
            (str(operation_id), str(run_id)))).fetchone()
        details = {"tool_name": row[0], "observation_scope": "runtime_local"}
        if observed is not None and observed.error is not None:
            details["category"] = observed.error.category
        summaries = {"tool.started": "工具已进入执行", "tool.succeeded": "工具返回成功结果",
            "tool.failed": "工具返回失败结果", "tool.waiting": "工具请求等待",
            "tool.unknown": "工具副作用结果无法确认"}
        event = RuntimeEvent(event_id=uuid4(), run_id=run_id, step_id=str(step_id),
            invocation_id=str(operation_id), event_type=event_type, occurred_at=now,
            summary=summaries[event_type], detail=RuntimeEventDetail(data=details))
        await self._append_event(db, run_id, event_type, event.model_dump(mode="json"), now,
            event_id=event.event_id)

    async def scan_expired(self, limit: int) -> list[AttemptRecord]:
        """Find expired leases and committed expirations awaiting a recovery decision."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute(
                "SELECT a.* FROM execution_attempts a JOIN execution_run_heads h "
                "ON h.run_id=a.run_id JOIN task_runs r ON r.id=a.run_id "
                "WHERE r.status='running' AND ((a.status='running' AND a.lease_expires_at<=? "
                "AND h.active_attempt_id=a.id) OR (a.status='expired' AND h.active_attempt_id IS NULL "
                "AND a.fencing_token=h.fencing_token AND NOT EXISTS "
                "(SELECT 1 FROM execution_attempts newer WHERE newer.run_id=a.run_id "
                "AND newer.attempt_no>a.attempt_no))) "
                "ORDER BY a.lease_expires_at, a.id LIMIT ?",
                (_dump_time(self._aware_now()), limit))).fetchall()
        return [_attempt_record(row) for row in rows]

    async def _candidate(self, db, run_id):
        run = await self._run_row(db, run_id)
        if run is None:
            raise KeyError(str(run_id))
        head = await self._head_row(db, run_id)
        attempt = await (await db.execute(
            "SELECT id FROM execution_attempts WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1",
            (str(run_id),))).fetchone()
        if head is None or attempt is None:
            raise RecoveryConflict("legacy_execution")
        checkpoint = await (await db.execute(
            "SELECT sequence, state FROM task_run_checkpoints WHERE run_id=?", (str(run_id),))).fetchone()
        raw = json.loads(checkpoint[1]) if checkpoint else None
        op = None
        if raw and raw.get("pending_operation_id"):
            cursor = await db.execute("SELECT * FROM execution_operations WHERE id=? AND run_id=?",
                                      (raw["pending_operation_id"], str(run_id)))
            row = await cursor.fetchone()
            if row:
                values = dict(zip([column[0] for column in cursor.description], row))
                values.pop("run_id")
                values.pop("result_ref")
                for key in ("input_payload", "result_payload", "before_digests", "after_digests"):
                    values[key] = json.loads(values[key]) if values[key] else (None if key == "result_payload" else {})
                values["id"] = UUID(values["id"])
                op = RecoveryOperation(**values)
        digest = canonical_digest({"head": list(head), "snapshot": raw,
                                   "operation": op.model_dump(mode="json") if op else None})
        return RecoveryCandidate(run_id=run_id, attempt_id=UUID(attempt[0]),
            run_version=run[6], fencing_token=head[0], checkpoint_sequence=checkpoint[0] if checkpoint else 0,
            snapshot=raw, operation=op, state_digest=digest)

    async def get_recovery_candidate(self, run_id: UUID) -> RecoveryCandidate:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN")
            return await self._candidate(db, run_id)

    async def expire_and_classify(self, attempt_id: UUID) -> RecoveryCandidate:
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            try:
                await db.execute("BEGIN IMMEDIATE")
                row = await (await db.execute("SELECT * FROM execution_attempts WHERE id=?",
                                              (str(attempt_id),))).fetchone()
                if row is None:
                    raise KeyError(str(attempt_id))
                attempt = _attempt_record(row)
                run = await self._run_row(db, attempt.run_id)
                head = await self._head_row(db, attempt.run_id)
                if (attempt.status == "expired" and run[4] == "running"
                        and head[1] is None and head[0] == attempt.fencing_token):
                    candidate = await self._candidate(db, attempt.run_id)
                    if candidate.attempt_id == attempt_id:
                        await db.rollback()
                        return candidate
                if (attempt.status != "running" or attempt.lease_expires_at > now
                        or head[1] != str(attempt_id) or run[4] != "running"):
                    raise RecoveryConflict("attempt_not_expired")
                await db.execute("UPDATE execution_attempts SET status='expired', finished_at=? WHERE id=?",
                                 (_dump_time(now), str(attempt_id)))
                calls = await (await db.execute("SELECT COALESCE(SUM(timeout_seconds),0) FROM execution_model_calls "
                    "WHERE lease_attempt_id=? AND status='reserved'", (str(attempt_id),))).fetchone()
                if calls[0]:
                    await db.execute("UPDATE execution_model_calls SET status='unknown',finished_at=? "
                        "WHERE lease_attempt_id=? AND status='reserved'", (_dump_time(now), str(attempt_id)))
                await self._settle_assistant_time(db, attempt.run_id, now, minimum=calls[0])
                checkpoint = await (await db.execute("SELECT state FROM task_run_checkpoints WHERE run_id=?",
                    (str(attempt.run_id),))).fetchone()
                if checkpoint and json.loads(checkpoint[0]).get("snapshot_type") == "assistant":
                    running = await (await db.execute("SELECT id, operation_id FROM execution_steps "
                        "WHERE attempt_id=? AND status='running'", (str(attempt_id),))).fetchall()
                    for step_id, operation_id in running:
                        await self._append_assistant_tool_event(db, attempt.run_id, operation_id,
                            step_id, "tool.unknown", now)
                await db.execute("UPDATE execution_operations SET status='unknown' WHERE status='running' "
                                 "AND id IN (SELECT operation_id FROM execution_steps WHERE attempt_id=? AND status='running')",
                                 (str(attempt_id),))
                await db.execute("UPDATE execution_steps SET status='unknown', finished_at=? WHERE attempt_id=? AND status='running'",
                                 (_dump_time(now), str(attempt_id)))
                await db.execute("UPDATE execution_run_heads SET active_attempt_id=NULL WHERE run_id=?",
                                 (str(attempt.run_id),))
                await self._append_event(db, attempt.run_id, "execution.expired",
                    {"attempt_id": str(attempt_id), "fencing_token": head[0]}, now)
                candidate = await self._candidate(db, attempt.run_id)
                await self._commit(db)
                return candidate
            except Exception:
                await db.rollback()
                raise

    async def apply_recovery(self, candidate: RecoveryCandidate, decision) -> RunRecord:
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            try:
                await db.execute("BEGIN IMMEDIATE")
                current = await self._candidate(db, candidate.run_id)
                run = await self._run_row(db, candidate.run_id)
                head = await self._head_row(db, candidate.run_id)
                if (current != candidate or run[4] not in {"running", "waiting"} or head[1] is not None):
                    raise RecoveryConflict("recovery_candidate_changed")
                verified = classify_recovery(current.snapshot, current.operation, decision.observation)
                if verified != decision:
                    raise RecoveryConflict("invalid_recovery_decision")
                if decision.action == "reject":
                    await db.rollback()
                    return _run_record(run)
                status = "waiting" if decision.action == "manual_review" else "queued"
                op = current.operation
                if op and decision.action in {"retry", "reconcile"}:
                    await db.execute("UPDATE execution_operations SET status=?, result_payload=? WHERE id=?",
                        ("prepared" if decision.action == "retry" else "succeeded",
                         json.dumps(decision.result_payload), str(op.id)))
                if status == "queued" and run[4] == "running":
                    await self._set_run_status(db, candidate.run_id, "waiting", now)
                await self._set_run_status(db, candidate.run_id, status, now,
                                           resume_confirmed=status == "queued")
                await self._append_event(db, candidate.run_id,
                    "execution.waiting" if status == "waiting" else "execution.recovered",
                    {"attempt_id": str(candidate.attempt_id), "fencing_token": candidate.fencing_token,
                     "operation_id": str(op.id) if op else None,
                     "reason_code": decision.reason_code, "action": decision.action}, now)
                state = dict(current.snapshot)
                sequence = await (await db.execute(
                    "SELECT MAX(sequence) FROM task_run_events WHERE run_id=?",
                    (str(candidate.run_id),))).fetchone()
                state["last_event_sequence"] = sequence[0]
                state["checkpoint_revision"] += 1
                await db.execute(
                    "UPDATE task_run_checkpoints SET sequence=sequence+1, state=?, created_at=? WHERE run_id=?",
                    (json.dumps(state), _dump_time(now), str(candidate.run_id)))
                await self._commit(db)
                return _run_record(await self._run_row(db, candidate.run_id))
            except Exception:
                await db.rollback()
                raise

    async def get_attempt(self, attempt_id: UUID) -> AttemptRecord:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT id, run_id, attempt_no, owner_id, fencing_token, status, "
                                      "lease_expires_at, heartbeat_at, started_at, finished_at, error_code "
                                      "FROM execution_attempts WHERE id=?", (str(attempt_id),))
            row = await cursor.fetchone()
        if row is None:
            raise KeyError(str(attempt_id))
        return _attempt_record(row)

    async def _validate_lease(self, db, lease: LeaseHandle, now: datetime) -> None:
        row = await (await db.execute(
            "SELECT h.active_attempt_id, a.owner_id, a.fencing_token, a.status, a.lease_expires_at, h.cancel_requested "
            "FROM execution_run_heads h JOIN execution_attempts a ON a.id=h.active_attempt_id "
            "WHERE h.run_id=?", (str(lease.run_id),))).fetchone()
        if (row is None or row[0] != str(lease.attempt_id) or row[1] != lease.owner_id or
                row[2] != lease.fencing_token or row[3] != "running" or row[5] or
                _parse_time(row[4]) <= now):
            raise StaleExecutionOwner("stale execution owner")

    async def _run_row(self, db, run_id: UUID):
        return await (await db.execute(
            f"SELECT {_RUN_COLUMNS} "
            "FROM task_runs WHERE id=?", (str(run_id),))).fetchone()

    async def _head_row(self, db, run_id: UUID):
        return await (await db.execute(
            "SELECT fencing_token, active_attempt_id FROM execution_run_heads WHERE run_id=?",
            (str(run_id),))).fetchone()

    async def _next_attempt_no(self, db, run_id: UUID) -> int:
        row = await (await db.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM execution_attempts WHERE run_id=?",
            (str(run_id),))).fetchone()
        return row[0]

    async def _set_run_status(self, db, run_id: UUID, status: str, now: datetime, *, resume_confirmed: bool = False) -> None:
        current = await self._run_row(db, run_id)
        if current is None:
            raise KeyError(str(run_id))
        transition_run(current[4], status, resume_confirmed=resume_confirmed)
        await db.execute("UPDATE task_runs SET status=?, version=version+1, updated_at=? WHERE id=?",
                         (status, _dump_time(now), str(run_id)))

    async def _append_event(self, db, run_id: UUID, event_type: str, payload: dict[str, Any], now: datetime,
                            *, event_id=None) -> None:
        row = await (await db.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_run_events WHERE run_id=?",
            (str(run_id),))).fetchone()
        await db.execute("INSERT INTO task_run_events (id, run_id, sequence, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                         (str(event_id or uuid4()), str(run_id), row[0], event_type,
                          json.dumps(_json_safe(payload)), _dump_time(now)))

    async def _configure(self, db) -> None:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=5000")

    async def _commit(self, db) -> None:
        await db.commit()

    def _aware_now(self) -> datetime:
        value = self.clock.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc)


def _assistant_audit_text(text):
    """Apply the existing keyed redaction policy to structured message text."""
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return text
    return json.dumps(_json_safe(value), ensure_ascii=False)


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


_RECOVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_schema_migrations (version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS execution_operations (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES task_runs(id),
    logical_index INTEGER NOT NULL, kind TEXT NOT NULL, tool_name TEXT,
    input_digest TEXT NOT NULL, input_payload TEXT NOT NULL, recovery_class TEXT NOT NULL,
    status TEXT NOT NULL, result_payload TEXT, result_ref TEXT,
    before_digests TEXT, after_digests TEXT, output_digest TEXT,
    UNIQUE(run_id, logical_index)
);
CREATE TABLE IF NOT EXISTS execution_steps (
    id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES execution_operations(id),
    attempt_id TEXT NOT NULL REFERENCES execution_attempts(id), execution_no INTEGER NOT NULL,
    status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, error_code TEXT, output_digest TEXT,
    UNIQUE(operation_id, execution_no)
);
CREATE INDEX IF NOT EXISTS idx_execution_operations_status ON execution_operations(run_id, status);
CREATE INDEX IF NOT EXISTS idx_execution_steps_attempt ON execution_steps(attempt_id);
INSERT OR IGNORE INTO execution_schema_migrations VALUES (1);
"""


def _dump_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _attempt_record(row) -> AttemptRecord:
    return AttemptRecord(id=UUID(row[0]), run_id=UUID(row[1]), attempt_no=row[2], owner_id=row[3],
                         fencing_token=row[4], status=row[5], lease_expires_at=_parse_time(row[6]),
                         heartbeat_at=_parse_time(row[7]), started_at=_parse_time(row[8]),
                         finished_at=_parse_time(row[9]) if row[9] else None, error_code=row[10])


def _run_record(row) -> RunRecord:
    return run_record_from_row(row)
