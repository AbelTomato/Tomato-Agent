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


def _bounded_title(value: str) -> str:
    first_line = str(value).splitlines()[0] if str(value).splitlines() else ""
    return " ".join(first_line.split())[:30].strip()


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
            columns = await db.execute_fetchall("PRAGMA table_info(sessions)")
            if "default_workspace_id" not in {column[1] for column in columns}:
                await db.execute("ALTER TABLE sessions ADD COLUMN default_workspace_id TEXT")
            for name, definition in (("title_source", "TEXT NOT NULL DEFAULT 'temporary'"),
                                     ("title_version", "INTEGER NOT NULL DEFAULT 0"),
                                     ("title_status", "TEXT NOT NULL DEFAULT 'idle'"),
                                     ("project_id", "TEXT")):
                if name not in {column[1] for column in columns}:
                    await db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")
            await db.commit()

    async def create_session(self, metadata: dict | None = None) -> UUID:
        ident = uuid4()
        timestamp = now()
        async with aiosqlite.connect(self.path) as db:
            await self._enable_foreign_keys(db)
            await db.execute(
                "INSERT INTO sessions (id, status, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
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
                "SELECT id, status, metadata, created_at, updated_at, default_workspace_id, "
                "title_source, title_version, title_status, project_id "
                "FROM sessions WHERE id = ?",
                (str(session_id),),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return SessionRecord(
            id=UUID(row[0]),
            status=row[1],
            metadata={**json.loads(row[2]), "title": json.loads(row[2]).get("title") or "未命名对话"},
            created_at=parse_datetime(row[3]),
            updated_at=parse_datetime(row[4]),
            default_workspace_id=row[5],
            title_source=row[6] or "temporary",
            title_version=row[7] or 0,
            title_status=row[8] or "idle",
            project_id=row[9],
        )

    async def list_sessions(self, limit=50, offset=0):
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                "SELECT id FROM sessions ORDER BY created_at, id LIMIT ? OFFSET ?", (limit, offset))
        sessions = [await self.get_session(UUID(row[0])) for row in rows]
        for session in sessions:
            if session is not None:
                session.metadata.setdefault("title", "未命名对话")
        return sessions

    async def set_default_workspace(self, session_id, workspace_id):
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "UPDATE sessions SET default_workspace_id=?, updated_at=? WHERE id=?",
                (workspace_id, now(), str(session_id)))
            if cursor.rowcount != 1:
                raise KeyError("session_not_found")
            await db.commit()
        return await self.get_session(session_id)

    async def ensure_temporary_title(self, session_id: UUID, title: str, project_id=None):
        title = _bounded_title(title) or "新对话"
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute(
                "SELECT metadata, title_source, project_id, title_version FROM sessions WHERE id=?",
                (str(session_id),))).fetchone()
            if row is None:
                raise KeyError("session_not_found")
            metadata = json.loads(row[0])
            changed = False
            if metadata.get("title") in {None, "", "未命名对话"} and row[3] == 0:
                metadata["title"] = title
                changed = True
            if project_id and row[2] is None:
                await db.execute("UPDATE sessions SET project_id=? WHERE id=?",
                                 (project_id, str(session_id)))
            if changed or (project_id and row[2] is None):
                await db.execute("UPDATE sessions SET metadata=?, title_source=?, "
                                 "title_status='idle', updated_at=? WHERE id=?",
                                 (json.dumps(metadata, ensure_ascii=False),
                                  "temporary" if changed else row[1], now(), str(session_id)))
            await db.commit()
        return await self.get_session(session_id)

    async def set_title(self, session_id: UUID, title: str, *, source: str = "manual",
                        expected_version: int | None = None):
        title = " ".join(str(title).splitlines()[0].split())[:30].strip()
        if not title:
            raise ValueError("title_required")
        if source not in {"manual", "model", "temporary"}:
            raise ValueError("invalid_title_source")
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute(
                "SELECT metadata, title_version FROM sessions WHERE id=?", (str(session_id),)
            )).fetchone()
            if row is None:
                raise KeyError("session_not_found")
            version = row[1] or 0
            if expected_version is not None and version != expected_version:
                raise ValueError("title_version_conflict")
            metadata = json.loads(row[0])
            metadata["title"] = title
            cursor = await db.execute(
                "UPDATE sessions SET metadata=?, title_source=?, title_version=?, title_status='ready', "
                "updated_at=? WHERE id=? AND title_version=?",
                (json.dumps(metadata, ensure_ascii=False), source, version + 1, now(),
                 str(session_id), version))
            if cursor.rowcount != 1:
                raise ValueError("title_version_conflict")
            await db.commit()
        return await self.get_session(session_id)

    async def set_generated_title(self, session_id: UUID, title: str, expected_version: int):
        title = _bounded_title(title)
        if not title:
            return False
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute(
                "SELECT metadata FROM sessions WHERE id=? AND title_version=? "
                "AND title_source!='manual'", (str(session_id), expected_version)
            )).fetchone()
            if row is None:
                return False
            metadata = json.loads(row[0])
            metadata["title"] = " ".join(title.splitlines()[0].split())[:30].strip()
            cursor = await db.execute(
                "UPDATE sessions SET metadata=?, title_source='model', title_version=title_version+1, "
                "title_status='ready', updated_at=? WHERE id=? AND title_version=? AND title_source!='manual'",
                (json.dumps(metadata, ensure_ascii=False), now(), str(session_id), expected_version))
            await db.commit()
            return cursor.rowcount == 1

    async def set_project(self, session_id: UUID, project_id: str | None):
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("UPDATE sessions SET project_id=?, updated_at=? WHERE id=?",
                                      (project_id, now(), str(session_id)))
            if cursor.rowcount != 1:
                raise KeyError("session_not_found")
            await db.commit()
        return await self.get_session(session_id)

    async def task_history_ids(self, session_id, limit=50, offset=0):
        """Merge unified and legacy records before paginating; never execute domains."""
        async with aiosqlite.connect(self.path) as db:
            return await db.execute_fetchall("""
                SELECT id, source FROM (
                    SELECT id, 'unified' AS source, created_at FROM task_runs WHERE session_id=?
                    UNION ALL
                    SELECT r.id, 'legacy_chat', r.created_at FROM runs r
                    WHERE r.session_id=? AND NOT EXISTS (
                        SELECT 1 FROM task_runs t WHERE t.domain_run_id=r.id)
                    AND NOT EXISTS (SELECT 1 FROM writing_tasks w
                        WHERE w.research_run_id=r.id OR w.drafting_run_id=r.id)
                    UNION ALL
                    SELECT w.task_id, 'legacy_writing', w.created_at FROM writing_tasks w
                    WHERE w.session_id=? AND NOT EXISTS (
                        SELECT 1 FROM task_runs t WHERE t.domain_run_id=w.task_id)
                ) ORDER BY created_at, id LIMIT ? OFFSET ?
            """, (str(session_id), str(session_id), str(session_id), limit, offset))

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

    async def list_completed_turn_events(self, session_id: UUID) -> list[Event]:
        """Return only user/final-assistant events from completed runs."""
        if await self.get_session(session_id) is None:
            raise ValueError(f"Session not found: {session_id}")

        async with aiosqlite.connect(self.path) as db:
            await self._enable_foreign_keys(db)
            cursor = await db.execute(
                "SELECT e.id, e.session_id, e.run_id, e.sequence, "
                "e.event_type, e.payload, e.created_at "
                "FROM events AS e "
                "JOIN runs AS r ON r.id = e.run_id "
                "WHERE e.session_id = ? AND r.status = 'completed' "
                "ORDER BY r.created_at ASC, r.id ASC, e.sequence ASC",
                (str(session_id),),
            )
            rows = await cursor.fetchall()

        events: list[Event] = []
        for row in rows:
            payload = json.loads(row[5])
            if row[4] == "user_message":
                include = True
            elif row[4] == "assistant_message":
                include = not payload.get("tool_calls")
            else:
                include = False
            if include:
                events.append(
                    Event(
                        id=UUID(row[0]),
                        session_id=UUID(row[1]),
                        run_id=UUID(row[2]),
                        sequence=row[3],
                        event_type=row[4],
                        payload=payload,
                        created_at=parse_datetime(row[6]),
                    )
                )
        return events

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
