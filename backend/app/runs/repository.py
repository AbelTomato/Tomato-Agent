import json
from collections.abc import Callable
from hashlib import sha256
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import aiosqlite

from .models import PendingAction, RunCheckpoint, RunEvent, RunRecord, RunRepositoryConflict, RunStatus
from .state_machine import transition_run
from .migrations import migrate_unified_runs
from .queue import SessionQueueRepository


_SENSITIVE_KEYS = {"authorization", "api_key", "credential", "password", "secret", "token"}
_UNSET = object()

_RUN_COLUMNS = (
    "id, task_type, request, workspace_id, status, error, version, created_at, updated_at, "
    "session_id, client_request_id, domain_run_id, result, stage, pending_action"
)


def run_record_from_row(row) -> RunRecord:
    return RunRecord(
        id=UUID(row[0]), task_type=row[1], request=json.loads(row[2]), workspace_id=row[3],
        status=row[4], error=json.loads(row[5]) if row[5] else None, version=row[6],
        created_at=_parse_datetime(row[7]), updated_at=_parse_datetime(row[8]),
        session_id=UUID(row[9]) if row[9] else None, client_request_id=row[10],
        domain_run_id=row[11], result=json.loads(row[12]) if row[12] else None,
        stage=row[13], pending_action=json.loads(row[14]) if row[14] else None,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).lower() in _SENSITIVE_KEYS else _json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


class RunRepository(SessionQueueRepository):
    def __init__(self, path: Path):
        self.path = Path(path)

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS task_runs (
                    id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    request TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_run_events (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES task_runs(id),
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS task_run_checkpoints (
                    run_id TEXT PRIMARY KEY REFERENCES task_runs(id),
                    sequence INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            await migrate_unified_runs(db)
            await db.execute("CREATE TABLE IF NOT EXISTS assistant_effect_settlements ("
                "invocation_id TEXT PRIMARY KEY, resolution TEXT NOT NULL, quarantined INTEGER NOT NULL DEFAULT 1, "
                "termination_evidence TEXT, conflict INTEGER NOT NULL DEFAULT 0)")
            await db.commit()

    async def create_run(
        self, task_type: str, request: dict[str, Any], workspace_id: str | None = None,
        *, snapshot_factory: Callable[[RunRecord], Any] | None = None,
        event_type: str | None = None, event_payload: dict[str, Any] | None = None,
    ) -> RunRecord:
        run_id = uuid4()
        timestamp = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO task_runs (id, task_type, request, workspace_id, status, error, version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(run_id),
                    task_type,
                    json.dumps(_json_safe(request)),
                    workspace_id,
                    "queued",
                    None,
                    0,
                    timestamp,
                    timestamp,
                ),
            )
            await self._initialize_run(db, run_id, timestamp, snapshot_factory,
                                       event_type, event_payload)
            await db.commit()
        return RunRecord(
            id=run_id,
            task_type=task_type,
            request=_json_safe(request),
            workspace_id=workspace_id,
            status="queued",
            version=0,
            created_at=_parse_datetime(timestamp),
            updated_at=_parse_datetime(timestamp),
        )

    async def get_run(self, run_id: str | UUID) -> RunRecord | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                f"SELECT {_RUN_COLUMNS} "
                "FROM task_runs WHERE id = ?",
                (str(run_id),),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return run_record_from_row(row)

    async def create_session_run(
        self, session_id: str | UUID, kind: str, request: dict[str, Any],
        workspace_id: str | None, client_request_id: str,
        *, snapshot_factory: Callable[[RunRecord], Any] | None = None,
        event_type: str | None = None, event_payload: dict[str, Any] | None = None,
        queued_request_id: str | None = None,
    ) -> RunRecord:
        if kind not in {"chat", "knowledge", "writing", "code", "assistant"}:
            raise ValueError("unsupported task kind")
        if kind == "code" and not workspace_id:
            raise ValueError("code requires workspace")
        if not client_request_id:
            raise ValueError("client_request_id must not be empty")
        digest = sha256(json.dumps(
            {"kind": kind, "request": request, "workspace_id": workspace_id},
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()).hexdigest()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            if not await (await db.execute("SELECT 1 FROM sessions WHERE id=?", (str(session_id),))).fetchone():
                raise KeyError(f"Session not found: {session_id}")
            existing = await (await db.execute(
                f"SELECT {_RUN_COLUMNS}, request_digest FROM task_runs WHERE session_id=? AND client_request_id=?",
                (str(session_id), client_request_id),
            )).fetchone()
            if existing:
                if existing[-1] != digest:
                    raise RunRepositoryConflict("request_conflict")
                return run_record_from_row(existing)
            if queued_request_id:
                queued = await (await db.execute("SELECT id FROM session_task_queue WHERE session_id=? AND status='pending' "
                    "ORDER BY sequence LIMIT 1", (str(session_id),))).fetchone()
                if not queued or queued[0] != queued_request_id:
                    raise RunRepositoryConflict("stale_action")
                latest = await (await db.execute("SELECT id,status FROM task_runs WHERE session_id=? "
                    "ORDER BY created_at DESC,id DESC LIMIT 1", (str(session_id),))).fetchone()
                control = await (await db.execute("SELECT paused,acknowledged_run_id FROM session_queue_control WHERE session_id=?",
                    (str(session_id),))).fetchone()
                if control and control[0] or latest and latest[1] in {"failed", "cancelled", "timed_out"} and (not control or control[1] != latest[0]):
                    raise RunRepositoryConflict("session_busy")
            if await (await db.execute(
                "SELECT 1 FROM task_runs WHERE session_id=? AND status IN ('queued','running','waiting')",
                (str(session_id),),
            )).fetchone():
                raise RunRepositoryConflict("session_busy")
            if kind in {"code", "assistant"} and workspace_id:
                if await (await db.execute(
                    "SELECT 1 FROM task_runs WHERE workspace_id=? AND status IN ('queued','running','waiting')",
                    (workspace_id,),
                )).fetchone():
                    raise RunRepositoryConflict("workspace_busy")
                operations = await (await db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_operations'"
                )).fetchone()
                if operations and await (await db.execute(
                    "SELECT 1 FROM execution_operations o JOIN task_runs r ON r.id=o.run_id "
                    "WHERE r.workspace_id=? AND o.recovery_class IN ('file_write','sandbox','artifact') "
                    "AND (o.status='running' OR (o.status='unknown' AND NOT EXISTS "
                    "(SELECT 1 FROM assistant_effect_settlements s WHERE s.invocation_id=o.id "
                    "AND s.quarantined=0 AND s.conflict=0 AND s.termination_evidence IS NOT NULL)))", (workspace_id,),
                )).fetchone():
                    raise RunRepositoryConflict("workspace_busy_unknown_outcome")
            run_id, timestamp = uuid4(), _now()
            await db.execute(
                "INSERT INTO task_runs (id, task_type, request, workspace_id, status, version, created_at, updated_at, "
                "session_id, client_request_id, request_digest) VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?)",
                (str(run_id), kind, json.dumps(_json_safe(request)), workspace_id, timestamp, timestamp,
                 str(session_id), client_request_id, digest),
            )
            await self._initialize_run(db, run_id, timestamp, snapshot_factory,
                                       event_type, event_payload)
            if queued_request_id:
                await db.execute("UPDATE session_task_queue SET status='started',run_id=? WHERE id=? AND status='pending'",
                    (str(run_id), queued_request_id))
            await db.commit()
        result = await self.get_run(run_id)
        assert result is not None
        return result

    async def _initialize_run(self, db, run_id, timestamp, snapshot_factory,
                              event_type, event_payload):
        """Keep the initial checkpoint and creation event in the Run transaction."""
        if snapshot_factory is not None:
            row = await (await db.execute(
                f"SELECT {_RUN_COLUMNS} FROM task_runs WHERE id=?", (str(run_id),)
            )).fetchone()
            snapshot = snapshot_factory(run_record_from_row(row))
            if snapshot.run_id != run_id:
                raise ValueError("snapshot_run_mismatch")
            await db.execute(
                "INSERT INTO task_run_checkpoints(run_id, sequence, state, created_at) "
                "VALUES (?, 1, ?, ?)",
                (str(run_id), snapshot.model_dump_json(), timestamp),
            )
        if event_type is not None:
            await db.execute(
                "INSERT INTO task_run_events(id, run_id, sequence, event_type, payload, created_at) "
                "VALUES (?, ?, 1, ?, ?, ?)",
                (str(uuid4()), str(run_id), event_type,
                 json.dumps(_json_safe(event_payload or {})), timestamp),
            )

    async def list_for_session(self, session_id: str | UUID, limit: int = 50, offset: int = 0) -> list[RunRecord]:
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset non-negative")
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute(
                f"SELECT {_RUN_COLUMNS} FROM task_runs WHERE session_id=? ORDER BY created_at, id LIMIT ? OFFSET ?",
                (str(session_id), limit, offset),
            )).fetchall()
        return [run_record_from_row(row) for row in rows]

    async def _reject_managed_write(self, db, run_id: str | UUID) -> None:
        table = await (await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_run_heads'"
        )).fetchone()
        if table is None:
            return
        head = await (await db.execute(
            "SELECT 1 FROM execution_run_heads WHERE run_id=?", (str(run_id),)
        )).fetchone()
        if head is not None:
            await db.rollback()
            raise RuntimeError("execution owner must commit managed runs")

    async def update_status(
        self,
        run_id: str | UUID,
        status: RunStatus,
        error: dict[str, Any] | None = None,
        expected_version: int | None = None,
        *, resume_confirmed: bool = False,
        result: dict[str, Any] | None | object = _UNSET,
        stage: str | None | object = _UNSET,
        pending_action: PendingAction | None | object = _UNSET,
        domain_run_id: str | None | object = _UNSET,
        event_type: str | None = None,
        event_payload: dict[str, Any] | None = None,
    ) -> RunRecord:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT status, version FROM task_runs WHERE id = ?", (str(run_id),)
            )
            row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                raise KeyError(f"Run not found: {run_id}")
            current_status, current_version = row
            await self._reject_managed_write(db, run_id)
            transition_run(current_status, status, resume_confirmed=resume_confirmed)
            if expected_version is not None and current_version != expected_version:
                await db.rollback()
                raise RunRepositoryConflict(f"Run version conflict: {run_id}")
            timestamp = _now()
            next_version = current_version + 1
            serialized_error = json.dumps(_json_safe(error)) if error is not None else None
            await db.execute(
                "UPDATE task_runs SET status = ?, error = ?, version = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, serialized_error, next_version, timestamp, str(run_id), current_version),
            )
            for column, value in (("result", result), ("stage", stage),
                                  ("pending_action", pending_action), ("domain_run_id", domain_run_id)):
                if value is _UNSET:
                    continue
                if isinstance(value, PendingAction):
                    value = value.model_dump(mode="json")
                if column in {"result", "pending_action"} and value is not None:
                    value = json.dumps(_json_safe(value))
                await db.execute(f"UPDATE task_runs SET {column}=? WHERE id=?", (value, str(run_id)))
            if event_type is not None:
                sequence = (await (await db.execute(
                    "SELECT COALESCE(MAX(sequence), 0)+1 FROM task_run_events WHERE run_id=?", (str(run_id),)
                )).fetchone())[0]
                await db.execute(
                    "INSERT INTO task_run_events (id, run_id, sequence, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (str(uuid4()), str(run_id), sequence, event_type, json.dumps(_json_safe(event_payload or {})), timestamp),
                )
            await db.commit()
        result = await self.get_run(run_id)
        assert result is not None
        return result

    async def append_event(
        self, run_id: str | UUID, event_type: str, payload: dict[str, Any]
    ) -> RunEvent:
        event_id = uuid4()
        timestamp = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await self._reject_managed_write(db, run_id)
            cursor = await db.execute(
                "SELECT 1 FROM task_runs WHERE id = ?",
                (str(run_id),),
            )
            row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                raise KeyError(f"Run not found: {run_id}")
            cursor = await db.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM task_run_events WHERE run_id = ?",
                (str(run_id),),
            )
            sequence = (await cursor.fetchone())[0] + 1
            await db.execute(
                "INSERT INTO task_run_events (id, run_id, sequence, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (str(event_id), str(run_id), sequence, event_type, json.dumps(_json_safe(payload)), timestamp),
            )
            await db.commit()
        return RunEvent(
            id=event_id,
            run_id=UUID(str(run_id)),
            sequence=sequence,
            event_type=event_type,
            payload=_json_safe(payload),
            created_at=_parse_datetime(timestamp),
        )

    async def get_event_cursor(self, run_id: str | UUID) -> int:
        """Return the latest persisted event sequence without loading event payloads."""
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM task_run_events WHERE run_id = ?",
                (str(run_id),),
            )
            return (await cursor.fetchone())[0]

    async def list_events(self, run_id: str | UUID, *, after: int = 0, limit: int | None = None) -> list[RunEvent]:
        if after < 0:
            raise ValueError("after must be non-negative")
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        if await self.get_run(run_id) is None:
            raise KeyError(f"Run not found: {run_id}")
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT id, run_id, sequence, event_type, payload, created_at "
                "FROM task_run_events WHERE run_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
                (str(run_id), after, limit if limit is not None else -1),
            )
            rows = await cursor.fetchall()
        return [
            RunEvent(
                id=UUID(row[0]),
                run_id=UUID(row[1]),
                sequence=row[2],
                event_type=row[3],
                payload=json.loads(row[4]),
                created_at=_parse_datetime(row[5]),
            )
            for row in rows
        ]

    async def save_checkpoint(self, run_id: str | UUID, state: dict[str, Any]) -> RunCheckpoint:
        timestamp = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await self._reject_managed_write(db, run_id)
            cursor = await db.execute(
                "SELECT 1 FROM task_runs WHERE id = ?",
                (str(run_id),),
            )
            row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                raise KeyError(f"Run not found: {run_id}")
            cursor = await db.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM task_run_checkpoints WHERE run_id = ?",
                (str(run_id),),
            )
            sequence = (await cursor.fetchone())[0] + 1
            await db.execute(
                "INSERT INTO task_run_checkpoints (run_id, sequence, state, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET sequence = excluded.sequence, state = excluded.state, created_at = excluded.created_at",
                (str(run_id), sequence, json.dumps(_json_safe(state)), timestamp),
            )
            await db.commit()
        return RunCheckpoint(
            run_id=UUID(str(run_id)),
            sequence=sequence,
            state=_json_safe(state),
            created_at=_parse_datetime(timestamp),
        )

    async def get_checkpoint(self, run_id: str | UUID) -> RunCheckpoint | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT sequence, state, created_at FROM task_run_checkpoints WHERE run_id = ?",
                (str(run_id),),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return RunCheckpoint(run_id=UUID(str(run_id)), sequence=row[0], state=json.loads(row[1]),
                             created_at=_parse_datetime(row[2]))
