import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import aiosqlite


def now():
    return datetime.now(timezone.utc).isoformat()


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

    async def session_exists(self, session_id: UUID) -> bool:
        async with aiosqlite.connect(self.path) as db:
            await self._enable_foreign_keys(db)
            cursor = await db.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (str(session_id),)
            )
            return await cursor.fetchone() is not None

    async def create_run(self, session_id: UUID) -> UUID:
        is_session_exist = await self.session_exists(session_id)
        if not is_session_exist:
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

    async def update_run(
        self, run_id: UUID, status: str, state: dict | None = None, loop_count: int = 0
    ):
        async with aiosqlite.connect(self.path) as db:
            await self._enable_foreign_keys(db)
            await db.execute(
                "UPDATE runs SET status=?, state=?, loop_count=?, updated_at=? WHERE id=?",
                (status, json.dumps(state or {}), loop_count, now(), str(run_id)),
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
            row = await cursor.fetchone();
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

    async def save_checkpoint(self, run_id: UUID, sequence: int, state: dict):
        async with aiosqlite.connect(self.path) as db:
            await self._enable_foreign_keys(db)
            cursor = await db.execute(
                "SELECT 1 FROM runs WHERE id = ?", (str(run_id),)
            )
            if await cursor.fetchone() is None:
                raise ValueError(f"Run not found: {run_id}")
            await db.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES (?, ?, ?, ?)",
                (str(run_id), sequence, json.dumps(state), now()),
            )
            await db.commit()
