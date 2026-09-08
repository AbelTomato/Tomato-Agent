import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import aiosqlite

from app.agent.models import CheckpointRecord, Event, RunRecord, SessionRecord


def now():
    return datetime.now(timezone.utc).isoformat()


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


class SessionRepository:
    def __init__(self, path: Path):
        self.path = path

    async def _enable_foreign_keys(self, db):
        await db.execute("PRAGMA foreign_keys = ON")

    async def init(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await self._enable_foreign_keys(db)
            await db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, status TEXT NOT NULL, metadata TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL, loop_count INTEGER NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY (session_id) REFERENCES sessions(id));
            CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(run_id, sequence), FOREIGN KEY (session_id) REFERENCES sessions(id), FOREIGN KEY (run_id) REFERENCES runs(id));
            CREATE TABLE IF NOT EXISTS checkpoints (run_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY (run_id) REFERENCES runs(id));
            """)
            await db.commit()

    async def create_session(self, metadata: dict | None = None) -> UUID:
        ident = uuid4()
        timestamp = now()
        async with aiosqlite.connect(self.path) as db:
            await self._enable_foreign_keys(db)
            await db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                (
                    str(ident),
                    "active",
                    json.dumps(metadata or {}),
                    timestamp,
                    timestamp,
                ),
            )
            await db.commit()
        return ident

    async def get_session(self, session_id: UUID) -> SessionRecord | None:
        async with aiosqlite.connect(self.path) as db:
            await self._enable_foreign_keys(db)
            cursor = await db.execute(
                "SELECT id, status, metadata, created_at, updated_at "
                "FROM sessions WHERE id = ?",
                (str(session_id),),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return SessionRecord(
            id=UUID(row[0]),
            status=row[1],
            metadata=json.loads(row[2]),
            created_at=parse_datetime(row[3]),
            updated_at=parse_datetime(row[4]),
        )

    async def session_exists(self, session_id: UUID) -> bool:
        return await self.get_session(session_id) is not None

    async def list_session_events(self, session_id: UUID) -> list[Event]:
        if await self.get_session(session_id) is None:
            raise ValueError(f"Session not found: {session_id}")
        async with aiosqlite.connect(self.path) as db:
            await self._enable_foreign_keys(db)
            cursor = await db.execute(
                "SELECT id, session_id, run_id, sequence, event_type, payload, created_at "
                "FROM events WHERE session_id = ? "
                "ORDER BY created_at ASC, run_id ASC, sequence ASC",
                (str(session_id),),
            )
            rows = await cursor.fetchall()
        return [
            Event(
                id=UUID(row[0]),
                session_id=UUID(row[1]),
                run_id=UUID(row[2]),
                sequence=row[3],
                event_type=row[4],
                payload=json.loads(row[5]),
                created_at=parse_datetime(row[6]),
            )
            for row in rows
        ]

    async def create_run(self, session_id: UUID) -> UUID:
        if not await self.session_exists(session_id):
            raise ValueError(f"Session not found: {session_id}")
        ident = uuid4()
        timestamp = now()
        async with aiosqlite.connect(self.path) as db:
            await self._enable_foreign_keys(db)
            await db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(ident), str(session_id), "running", 0, "{}", timestamp, timestamp),
            )
            await db.commit()
        return ident

    async def get_run(
        self, run_id: UUID, session_id: UUID | None = None
    ) -> RunRecord | None:
        async with aiosqlite.connect(self.path) as db:
            await self._enable_foreign_keys(db)
            cursor = await db.execute(
                "SELECT id, session_id, status, loop_count, state, created_at, updated_at "
                "FROM runs WHERE id = ?",
                (str(run_id),),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        if session_id is not None and row[1] != str(session_id):
            raise ValueError(f"Run {run_id} does not belong to session {session_id}")
        return RunRecord(
            id=UUID(row[0]),
            session_id=UUID(row[1]),
            status=row[2],
            loop_count=row[3],
            state=json.loads(row[4]),
            created_at=parse_datetime(row[5]),
            updated_at=parse_datetime(row[6]),
        )

    async def update_run(
        self, run_id: UUID, status: str, state: dict | None = None, loop_count: int = 0
    ):
        if loop_count < 0:
            raise ValueError("loop_count cannot be negative")
        async with aiosqlite.connect(self.path) as db:
            await self._enable_foreign_keys(db)
            cursor = await db.execute("SELECT 1 FROM runs WHERE id = ?", (str(run_id),))
            if await cursor.fetchone() is None:
                raise ValueError(f"Run not found: {run_id}")
            await db.execute(
                "UPDATE runs SET status=?, state=?, loop_count=?, updated_at=? WHERE id=?",
                (
                    status,
                    json.dumps(state if state is not None else {}),
                    loop_count,
                    now(),
                    str(run_id),
                ),
            )
            await db.commit()

    async def append_event(
        self, session_id: UUID, run_id: UUID, event_type: str, payload: dict
    ) -> int:
        async with aiosqlite.connect(self.path) as db:
            await self._enable_foreign_keys(db)
            cursor = await db.execute(
                "SELECT session_id FROM runs WHERE id = ?", (str(run_id),)
            )
            run = await cursor.fetchone()
            if run is None:
                raise ValueError(f"Run not found: {run_id}")
            if run[0] != str(session_id):
                raise ValueError(
                    f"Run {run_id} does not belong to session {session_id}"
                )
            cursor = await db.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE run_id=?",
                (str(run_id),),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("Failed to calculate next event sequence")
            sequence = row[0]
            await db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid4()),
                    str(session_id),
                    str(run_id),
                    sequence,
                    event_type,
                    json.dumps(payload),
                    now(),
                ),
            )
            await db.commit()
            return sequence

    async def list_events(self, run_id: UUID, after_sequence: int = 0) -> list[Event]:
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        if await self.get_run(run_id) is None:
            raise ValueError(f"Run not found: {run_id}")
        async with aiosqlite.connect(self.path) as db:
            await self._enable_foreign_keys(db)
            cursor = await db.execute(
                "SELECT id, session_id, run_id, sequence, event_type, payload, created_at "
                "FROM events WHERE run_id = ? AND sequence > ? ORDER BY sequence ASC",
                (str(run_id), after_sequence),
            )
            rows = await cursor.fetchall()
        return [
            Event(
                id=UUID(row[0]),
                session_id=UUID(row[1]),
                run_id=UUID(row[2]),
                sequence=row[3],
                event_type=row[4],
                payload=json.loads(row[5]),
                created_at=parse_datetime(row[6]),
            )
            for row in rows
        ]

    async def save_checkpoint(self, run_id: UUID, sequence: int, state: dict):
        if sequence < 0:
            raise ValueError("sequence cannot be negative")
        async with aiosqlite.connect(self.path) as db:
            await self._enable_foreign_keys(db)
            cursor = await db.execute("SELECT 1 FROM runs WHERE id = ?", (str(run_id),))
            if await cursor.fetchone() is None:
                raise ValueError(f"Run not found: {run_id}")
            await db.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES (?, ?, ?, ?)",
                (str(run_id), sequence, json.dumps(state), now()),
            )
            await db.commit()

    async def get_checkpoint(self, run_id: UUID) -> CheckpointRecord | None:
        if await self.get_run(run_id) is None:
            raise ValueError(f"Run not found: {run_id}")
        async with aiosqlite.connect(self.path) as db:
            await self._enable_foreign_keys(db)
            cursor = await db.execute(
                "SELECT run_id, sequence, state, created_at "
                "FROM checkpoints WHERE run_id = ?",
                (str(run_id),),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return CheckpointRecord(
            run_id=UUID(row[0]),
            sequence=row[1],
            state=json.loads(row[2]),
            created_at=parse_datetime(row[3]),
        )
