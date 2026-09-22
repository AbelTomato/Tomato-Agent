import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import aiosqlite

from app.knowledge.service import CitationSnapshot

from .models import WritingStatus, WritingTask


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


class WritingRepository:
    def __init__(self, path: Path):
        self.path = path

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS writing_tasks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    outline TEXT NOT NULL,
                    draft TEXT,
                    citations TEXT NOT NULL,
                    research_run_id TEXT,
                    drafting_run_id TEXT,
                    saved_path TEXT,
                    failed_stage TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
                """
            )
            columns = await db.execute_fetchall("PRAGMA table_info(writing_tasks)")
            if "saved_path" not in {column[1] for column in columns}:
                await db.execute("ALTER TABLE writing_tasks ADD COLUMN saved_path TEXT")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS writing_saves (
                    task_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    saved_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, idempotency_key),
                    FOREIGN KEY (task_id) REFERENCES writing_tasks(task_id)
                )
                """
            )
            await db.commit()

    async def create_task(self, session_id: UUID, topic: str) -> WritingTask:
        task_id = uuid4()
        timestamp = _now()
        try:
            async with aiosqlite.connect(self.path) as db:
                await db.execute("PRAGMA foreign_keys = ON")
                await db.execute(
                    """
                    INSERT INTO writing_tasks (
                        task_id, session_id, topic, status, version, outline,
                        draft, citations, research_run_id, drafting_run_id,
                        saved_path, failed_stage, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(task_id),
                        str(session_id),
                        topic,
                        WritingStatus.RESEARCHING.value,
                        1,
                        "{}",
                        None,
                        "[]",
                        None,
                        None,
                        None,
                        None,
                        timestamp,
                        timestamp,
                    ),
                )
                await db.commit()
        except aiosqlite.IntegrityError as exc:
            raise ValueError(f"Session not found: {session_id}") from exc
        task = await self.get_task(task_id)
        assert task is not None
        return task

    async def get_task(self, task_id: UUID) -> WritingTask | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT task_id, session_id, topic, status, version, outline,
                       draft, citations, research_run_id, drafting_run_id,
                       saved_path, failed_stage, created_at, updated_at
                FROM writing_tasks WHERE task_id = ?
                """,
                (str(task_id),),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return WritingTask(
            task_id=UUID(row[0]),
            session_id=UUID(row[1]),
            topic=row[2],
            status=WritingStatus(row[3]),
            version=row[4],
            outline=json.loads(row[5]),
            draft=row[6],
            citations=[CitationSnapshot.model_validate(item) for item in json.loads(row[7])],
            research_run_id=UUID(row[8]) if row[8] else None,
            drafting_run_id=UUID(row[9]) if row[9] else None,
            saved_path=row[10],
            failed_stage=row[11],
            created_at=_parse_datetime(row[12]),
            updated_at=_parse_datetime(row[13]),
        )

    async def transition(
        self,
        task_id: UUID,
        *,
        expected_status: WritingStatus,
        expected_version: int,
        status: WritingStatus,
        outline: dict[str, Any] | None = None,
        citations: list[CitationSnapshot] | None = None,
        research_run_id: UUID | None = None,
        draft: str | None = None,
        drafting_run_id: UUID | None = None,
    ) -> WritingTask:
        timestamp = _now()
        values = {
            "status": status.value,
            "version": expected_version + 1,
            "outline": json.dumps(outline if outline is not None else {}, ensure_ascii=False),
            "citations": json.dumps(
                [item.model_dump(mode="json") for item in (citations or [])],
                ensure_ascii=False,
            ),
            "research_run_id": str(research_run_id) if research_run_id else None,
            "updated_at": timestamp,
        }
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                UPDATE writing_tasks
                SET status = ?, version = ?, outline = ?, citations = ?,
                    research_run_id = ?, draft = ?, drafting_run_id = ?, updated_at = ?
                WHERE task_id = ? AND status = ? AND version = ?
                """,
                (
                    values["status"],
                    values["version"],
                    values["outline"],
                    values["citations"],
                    values["research_run_id"],
                    draft,
                    str(drafting_run_id) if drafting_run_id else None,
                    values["updated_at"],
                    str(task_id),
                    expected_status.value,
                    expected_version,
                ),
            )
            await db.commit()
            if cursor.rowcount != 1:
                raise RuntimeError("writing task transition conflict")
        task = await self.get_task(task_id)
        assert task is not None
        return task

    async def save_task(
        self,
        task_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
        saved_path: str,
    ) -> WritingTask:
        timestamp = _now()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT saved_path FROM writing_saves WHERE task_id = ? AND idempotency_key = ?",
                (str(task_id), idempotency_key),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                task = await self.get_task(task_id)
                if task is None:
                    raise RuntimeError("writing task not found")
                return task
            cursor = await db.execute(
                """
                UPDATE writing_tasks
                SET status = ?, version = ?, saved_path = ?, updated_at = ?
                WHERE task_id = ? AND status = ? AND version = ?
                """,
                (
                    WritingStatus.SAVED.value,
                    expected_version + 1,
                    saved_path,
                    timestamp,
                    str(task_id),
                    WritingStatus.AWAITING_SAVE_CONFIRMATION.value,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise RuntimeError("writing task save conflict")
            await db.execute(
                """
                INSERT INTO writing_saves (task_id, idempotency_key, version, saved_path, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(task_id), idempotency_key, expected_version + 1, saved_path, timestamp),
            )
            await db.commit()
        task = await self.get_task(task_id)
        assert task is not None
        return task

    async def has_save_key(self, task_id: UUID, idempotency_key: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM writing_saves WHERE task_id = ? AND idempotency_key = ?",
                (str(task_id), idempotency_key),
            )
            return await cursor.fetchone() is not None

    async def mark_failed(
        self,
        task_id: UUID,
        *,
        expected_version: int,
        failed_stage: str,
    ) -> WritingTask:
        timestamp = _now()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                UPDATE writing_tasks
                SET status = ?, version = ?, failed_stage = ?, updated_at = ?
                WHERE task_id = ? AND version = ? AND status != ?
                """,
                (
                    WritingStatus.FAILED.value,
                    expected_version + 1,
                    failed_stage,
                    timestamp,
                    str(task_id),
                    expected_version,
                    WritingStatus.SAVED.value,
                ),
            )
            await db.commit()
            if cursor.rowcount != 1:
                raise RuntimeError("writing task failure transition conflict")
        task = await self.get_task(task_id)
        assert task is not None
        return task

    async def retry_generation(
        self,
        task_id: UUID,
        *,
        expected_version: int,
        status: WritingStatus,
    ) -> WritingTask:
        timestamp = _now()
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                UPDATE writing_tasks
                SET status = ?, version = ?, failed_stage = NULL, updated_at = ?
                WHERE task_id = ? AND status = ? AND version = ? AND failed_stage = ?
                """,
                (
                    status.value,
                    expected_version + 1,
                    timestamp,
                    str(task_id),
                    WritingStatus.FAILED.value,
                    expected_version,
                    status.value,
                ),
            )
            await db.commit()
            if cursor.rowcount != 1:
                raise RuntimeError("writing task retry conflict")
        task = await self.get_task(task_id)
        assert task is not None
        return task
