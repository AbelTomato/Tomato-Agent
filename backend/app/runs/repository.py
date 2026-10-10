import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import aiosqlite

from .models import RunCheckpoint, RunEvent, RunRecord, RunRepositoryConflict, RunStatus


_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    "queued": {"queued", "running", "cancelled", "timed_out", "failed"},
    "running": {"running", "waiting", "completed", "failed", "cancelled", "timed_out"},
    "waiting": {"waiting", "running", "completed", "failed", "cancelled", "timed_out"},
    "completed": {"completed"},
    "failed": {"failed"},
    "cancelled": {"cancelled"},
    "timed_out": {"timed_out"},
}
_SENSITIVE_KEYS = {"authorization", "api_key", "credential", "password", "secret", "token"}


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


class RunRepository:
    def __init__(self, path: Path):
        self.path = Path(path)

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
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
            await db.commit()

    async def create_run(
        self, task_type: str, request: dict[str, Any], workspace_id: str
    ) -> RunRecord:
        run_id = uuid4()
        timestamp = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO task_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                "SELECT id, task_type, request, workspace_id, status, error, version, created_at, updated_at "
                "FROM task_runs WHERE id = ?",
                (str(run_id),),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return RunRecord(
            id=UUID(row[0]),
            task_type=row[1],
            request=json.loads(row[2]),
            workspace_id=row[3],
            status=row[4],
            error=json.loads(row[5]) if row[5] else None,
            version=row[6],
            created_at=_parse_datetime(row[7]),
            updated_at=_parse_datetime(row[8]),
        )

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
            if status not in _TRANSITIONS[current_status]:
                await db.rollback()
                raise ValueError(f"Invalid run status transition: {current_status} -> {status}")
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
                "INSERT INTO task_run_events VALUES (?, ?, ?, ?, ?, ?)",
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

    async def list_events(self, run_id: str | UUID) -> list[RunEvent]:
        if await self.get_run(run_id) is None:
            raise KeyError(f"Run not found: {run_id}")
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT id, run_id, sequence, event_type, payload, created_at "
                "FROM task_run_events WHERE run_id = ? ORDER BY sequence",
                (str(run_id),),
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
                "INSERT INTO task_run_checkpoints VALUES (?, ?, ?, ?) "
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
