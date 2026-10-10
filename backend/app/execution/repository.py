from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import aiosqlite

from app.runs.models import RunRecord

from .models import (AttemptRecord, LeaseHandle, PROTOCOL_VERSION, ExecutionSnapshot,
                     OperationInput, StepRecord, StepOutcome)
from .ports import Clock, canonical_digest, execution_migration
from .state_machine import StaleExecutionOwner
from .recovery import RecoveryCandidate, RecoveryConflict, RecoveryOperation, classify_recovery


class ExecutionRepository:
    def __init__(self, path: Path, *, clock: Clock | None = None) -> None:
        self.path = Path(path)
        self.clock = clock or _SystemClock()

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            await db.executescript(";".join(execution_migration()) + ";")
            await db.executescript(_RECOVERY_SCHEMA)
            await db.commit()

    async def claim(
        self, run_id: UUID, owner_id: str, *, lease_seconds: int
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

    async def request_cancel(self, run_id: UUID) -> RunRecord:
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
                     error: dict[str, Any] | None = None) -> RunRecord:
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._validate_lease(db, lease, now)
                row = await self._run_row(db, lease.run_id)
                if row[4] == "cancelled":
                    raise StaleExecutionOwner("run has been cancelled")
                await self._set_run_status(db, lease.run_id, status, now)
                await db.execute("UPDATE task_runs SET error=? WHERE id=?",
                                 (json.dumps(error) if error else None, str(lease.run_id)))
                if snapshot is not None:
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
                    ("succeeded" if status == "completed" else "cancelled" if status == "cancelled" else "failed",
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
        from .models import ExecutionSnapshot
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute("SELECT state FROM task_run_checkpoints WHERE run_id=?",
                                          (str(run_id),))).fetchone()
        return ExecutionSnapshot.model_validate_json(row[0]) if row else None

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
                                     start=None, finish_step=None, event_type=None):
        if snapshot.run_id != lease.run_id:
            raise RecoveryConflict("snapshot_run_mismatch")
        now = self._aware_now()
        async with aiosqlite.connect(self.path) as db:
            await self._configure(db)
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._validate_lease(db, lease, now)
                payload = {"attempt_id": str(lease.attempt_id), "fencing_token": lease.fencing_token}
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
                    op = await (await db.execute(
                        "SELECT status FROM execution_operations WHERE id=? AND run_id=?",
                        (str(operation_id), str(lease.run_id)))).fetchone()
                    if op is None:
                        raise KeyError(str(operation_id))
                    if op[0] != "prepared":
                        raise RecoveryConflict("invalid_transition")
                    number = await (await db.execute(
                        "SELECT COALESCE(MAX(execution_no),0)+1 FROM execution_steps WHERE operation_id=?",
                        (str(operation_id),))).fetchone()
                    await db.execute("INSERT INTO execution_steps "
                        "(id, operation_id, attempt_id, execution_no, status, started_at) VALUES (?, ?, ?, ?, 'running', ?)",
                        (str(step_id), str(operation_id), str(lease.attempt_id), number[0], _dump_time(now)))
                    await db.execute("UPDATE execution_operations SET status='running' WHERE id=?", (str(operation_id),))
                    payload.update(operation_id=str(operation_id), step_id=str(step_id))
                    event_type = "step.started"
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
                    digest = canonical_digest(outcome.result_payload)
                    await db.execute("UPDATE execution_steps SET status=?, finished_at=?, output_digest=? WHERE id=?",
                                     (status, _dump_time(now), digest, str(step_id)))
                    await db.execute("UPDATE execution_operations SET status=?, result_payload=?, output_digest=? WHERE id=?",
                                     (status, json.dumps(outcome.result_payload), digest, row[0]))
                    payload.update(operation_id=row[0], step_id=str(step_id))
                    event_type = "step." + status
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

    async def scan_expired(self, limit: int) -> list[AttemptRecord]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute(
                "SELECT a.* FROM execution_attempts a JOIN execution_run_heads h "
                "ON h.active_attempt_id=a.id JOIN task_runs r ON r.id=a.run_id "
                "WHERE a.status='running' AND a.lease_expires_at<=? AND r.status='running' "
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
                if (attempt.status != "running" or attempt.lease_expires_at > now
                        or head[1] != str(attempt_id) or run[4] != "running"):
                    raise RecoveryConflict("attempt_not_expired")
                await db.execute("UPDATE execution_attempts SET status='expired', finished_at=? WHERE id=?",
                                 (_dump_time(now), str(attempt_id)))
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
                await self._set_run_status(db, candidate.run_id, status, now)
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
            "SELECT id, task_type, request, workspace_id, status, error, version, created_at, updated_at "
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

    async def _set_run_status(self, db, run_id: UUID, status: str, now: datetime) -> None:
        await db.execute("UPDATE task_runs SET status=?, version=version+1, updated_at=? WHERE id=?",
                         (status, _dump_time(now), str(run_id)))

    async def _append_event(self, db, run_id: UUID, event_type: str, payload: dict[str, Any], now: datetime) -> None:
        row = await (await db.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_run_events WHERE run_id=?",
            (str(run_id),))).fetchone()
        await db.execute("INSERT INTO task_run_events VALUES (?, ?, ?, ?, ?, ?)",
                         (str(uuid4()), str(run_id), row[0], event_type, json.dumps(payload), _dump_time(now)))

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
    return RunRecord(id=UUID(row[0]), task_type=row[1], request=json.loads(row[2]), workspace_id=row[3],
                     status=row[4], error=json.loads(row[5]) if row[5] else None, version=row[6],
                     created_at=_parse_time(row[7]), updated_at=_parse_time(row[8]))