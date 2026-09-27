import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import aiosqlite

from app.writing.models import WritingStatus, WritingTask
from app.writing.service import WritingConflictError, WritingNotFoundError

from .execution_models import (
    DraftConfig,
    ExecutionAttempt,
    ExecutionConfig,
    GeneratedDraft,
    GeneratedOutline,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WritingExecutionRepository:
    def __init__(self, path: Path):
        self.path = Path(path)

    @asynccontextmanager
    async def _connect(self):
        async with aiosqlite.connect(self.path, timeout=5) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            yield db

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS writing_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES writing_tasks(task_id),
                    kind TEXT NOT NULL DEFAULT 'research' CHECK(kind IN ('research','draft')),
                    input_version INTEGER NOT NULL,
                    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
                    status TEXT NOT NULL CHECK(status IN ('running','completed','failed','conflicted')),
                    phase TEXT NOT NULL CHECK(phase IN ('retrieval','generation','validation','publication')),
                    error_code TEXT,
                    config TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(task_id, kind, input_version)
                )
                """
            )
            columns = await db.execute_fetchall("PRAGMA table_info(writing_attempts)")
            if not any(column[1] == "kind" for column in columns):
                await db.execute("ALTER TABLE writing_attempts RENAME TO writing_attempts_legacy")
                await db.execute(
                    """
                    CREATE TABLE writing_attempts (
                        attempt_id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL REFERENCES writing_tasks(task_id),
                        kind TEXT NOT NULL CHECK(kind IN ('research','draft')),
                        input_version INTEGER NOT NULL,
                        run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
                        status TEXT NOT NULL CHECK(status IN ('running','completed','failed','conflicted')),
                        phase TEXT NOT NULL CHECK(phase IN ('retrieval','generation','validation','publication')),
                        error_code TEXT,
                        config TEXT NOT NULL,
                        model_id TEXT NOT NULL,
                        prompt_version TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(task_id, kind, input_version)
                    )
                    """
                )
                await db.execute(
                    """
                    INSERT INTO writing_attempts
                    (attempt_id,task_id,kind,input_version,run_id,status,phase,error_code,config,model_id,prompt_version,created_at,updated_at)
                    SELECT attempt_id,task_id,'research',input_version,run_id,status,phase,error_code,config,model_id,prompt_version,created_at,updated_at
                    FROM writing_attempts_legacy
                    """
                )
                await db.execute("DROP TABLE writing_attempts_legacy")
            await db.commit()

    @staticmethod
    def _attempt(row) -> ExecutionAttempt:
        return ExecutionAttempt(
            attempt_id=UUID(row[0]), task_id=UUID(row[1]), kind=row[2], input_version=row[3],
            run_id=UUID(row[4]), status=row[5], phase=row[6], error_code=row[7],
            config=(DraftConfig if row[2] == "draft" else ExecutionConfig).model_validate_json(row[8]),
            model_id=row[9], prompt_version=row[10], created_at=datetime.fromisoformat(row[11]),
            updated_at=datetime.fromisoformat(row[12]),
        )

    _ATTEMPT_COLUMNS = (
        "attempt_id,task_id,kind,input_version,run_id,status,phase,error_code,config,"
        "model_id,prompt_version,created_at,updated_at"
    )

    @staticmethod
    def _task(row) -> WritingTask:
        return WritingTask(
            task_id=UUID(row[0]), session_id=UUID(row[1]), topic=row[2], status=row[3],
            version=row[4], outline=json.loads(row[5]), draft=row[6],
            citations=json.loads(row[7]), research_run_id=UUID(row[8]) if row[8] else None,
            drafting_run_id=UUID(row[9]) if row[9] else None, saved_path=row[10],
            failed_stage=row[11], created_at=datetime.fromisoformat(row[12]),
            updated_at=datetime.fromisoformat(row[13]),
        )

    async def get_attempt(self, task_id: UUID, input_version: int, *, kind: str = "research") -> ExecutionAttempt | None:
        async with self._connect() as db:
            cursor = await db.execute(
                f"SELECT {self._ATTEMPT_COLUMNS} FROM writing_attempts WHERE task_id=? AND kind=? AND input_version=?",
                (str(task_id), kind, input_version),
            )
            row = await cursor.fetchone()
        return self._attempt(row) if row else None

    async def get_latest_attempt(
        self, task_id: UUID, *, kind: str = "research"
    ) -> ExecutionAttempt | None:
        async with self._connect() as db:
            cursor = await db.execute(
                f"SELECT {self._ATTEMPT_COLUMNS} FROM writing_attempts "
                "WHERE task_id=? AND kind=? ORDER BY input_version DESC LIMIT 1",
                (str(task_id), kind),
            )
            row = await cursor.fetchone()
        return self._attempt(row) if row else None

    async def get_attempt_by_id(self, attempt_id: UUID) -> ExecutionAttempt | None:
        async with self._connect() as db:
            cursor = await db.execute(
                f"SELECT {self._ATTEMPT_COLUMNS} FROM writing_attempts WHERE attempt_id=?",
                (str(attempt_id),),
            )
            row = await cursor.fetchone()
        return self._attempt(row) if row else None

    async def start_attempt(
        self, task_id: UUID, *, expected_version: int,
        config: ExecutionConfig, model_id: str, attempt_id: UUID | None = None,
    ) -> ExecutionAttempt:
        attempt_id = attempt_id or uuid4()
        run_id = uuid4()
        timestamp = _now()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT session_id,status,version FROM writing_tasks WHERE task_id=?",
                (str(task_id),),
            )
            task = await cursor.fetchone()
            if task is None:
                await db.rollback()
                raise WritingNotFoundError(f"Writing task not found: {task_id}")
            if task[1] != WritingStatus.RESEARCHING.value or task[2] != expected_version:
                await db.rollback()
                raise WritingConflictError("writing task is not at the expected researching version")
            try:
                await db.execute(
                    "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(run_id), task[0], "running", 0,
                     json.dumps({"task_id": str(task_id), "attempt_id": str(attempt_id), "phase": "retrieval"}),
                     timestamp, timestamp),
                )
                await db.execute(
                    "INSERT INTO writing_attempts "
                    "(attempt_id,task_id,kind,input_version,run_id,status,phase,error_code,config,model_id,prompt_version,created_at,updated_at) "
                    "VALUES (?, ?, 'research', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(attempt_id), str(task_id), expected_version, str(run_id), "running",
                     "retrieval", None, config.model_dump_json(), model_id,
                     "writing-outline/v1", timestamp, timestamp),
                )
                await db.commit()
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise WritingConflictError("writing attempt already exists") from exc
        result = await self.get_attempt(task_id, expected_version)
        assert result is not None
        return result

    async def start_draft_attempt(
        self, task_id: UUID, *, expected_version: int,
        config: DraftConfig, model_id: str, attempt_id: UUID | None = None,
    ) -> ExecutionAttempt:
        attempt_id = attempt_id or uuid4()
        run_id = uuid4()
        timestamp = _now()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            task = await (await db.execute(
                "SELECT session_id,status,version FROM writing_tasks WHERE task_id=?", (str(task_id),)
            )).fetchone()
            if task is None:
                await db.rollback()
                raise WritingNotFoundError(f"Writing task not found: {task_id}")
            if task[1] != WritingStatus.DRAFTING.value or task[2] != expected_version:
                await db.rollback()
                raise WritingConflictError("writing task is not at the expected drafting version")
            try:
                await db.execute(
                    "INSERT INTO runs VALUES (?, ?, 'running', 0, ?, ?, ?)",
                    (str(run_id), task[0], json.dumps({"task_id": str(task_id), "attempt_id": str(attempt_id), "phase": "generation"}), timestamp, timestamp),
                )
                await db.execute(
                    "INSERT INTO writing_attempts "
                    "(attempt_id,task_id,kind,input_version,run_id,status,phase,error_code,config,model_id,prompt_version,created_at,updated_at) "
                    "VALUES (?, ?, 'draft', ?, ?, 'running', 'generation', NULL, ?, ?, 'writing-draft/v1', ?, ?)",
                    (str(attempt_id), str(task_id), expected_version, str(run_id), config.model_dump_json(), model_id, timestamp, timestamp),
                )
                await db.commit()
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise WritingConflictError("draft attempt already exists") from exc
        result = await self.get_attempt(task_id, expected_version, kind="draft")
        assert result is not None
        return result

    async def get_run_for_attempt(self, attempt_id: UUID) -> dict | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT r.id,r.session_id,r.status,r.state FROM runs r "
                "JOIN writing_attempts a ON a.run_id=r.id WHERE a.attempt_id=?",
                (str(attempt_id),),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return {"id": row[0], "session_id": row[1], "status": row[2], "state": json.loads(row[3])}

    async def complete_draft_attempt(self, attempt_id: UUID, *, draft: str) -> WritingTask:
        conflict = False
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            attempt, task, run = await self._load_attempt_task(db, attempt_id)
            if attempt.kind != "draft":
                await db.rollback()
                raise WritingConflictError("attempt is not a draft attempt")
            if attempt.status != "running" or run[1] != "running":
                await db.rollback()
                raise WritingConflictError("attempt or run is not running")
            if task.status != WritingStatus.DRAFTING or task.version != attempt.input_version:
                await self._mark_stale(db, attempt)
                await db.commit()
                conflict = True
            else:
                timestamp = _now()
                cursor = await db.execute(
                    "UPDATE writing_tasks SET status=?,version=?,draft=?,drafting_run_id=?,"
                    "failed_stage=NULL,updated_at=? WHERE task_id=? AND status=? AND version=?",
                    (WritingStatus.AWAITING_SAVE_CONFIRMATION.value, attempt.input_version + 1,
                     draft, str(attempt.run_id), timestamp, str(task.task_id),
                     WritingStatus.DRAFTING.value, attempt.input_version),
                )
                if cursor.rowcount != 1:
                    await self._mark_stale(db, attempt)
                    await db.commit()
                    conflict = True
                else:
                    await db.execute(
                        "UPDATE writing_attempts SET status='completed',phase='publication',updated_at=? "
                        "WHERE attempt_id=? AND status='running'", (timestamp, str(attempt_id)),
                    )
                    await db.execute(
                        "UPDATE runs SET status='completed',state=?,updated_at=? WHERE id=? AND status='running'",
                        (json.dumps({"task_id": str(task.task_id), "attempt_id": str(attempt_id),
                                     "phase": "publication", "result": "draft_published"}),
                         timestamp, str(attempt.run_id)),
                    )
                    await db.commit()
        if conflict:
            raise WritingConflictError("writing task changed before draft publication")
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT task_id,session_id,topic,status,version,outline,draft,citations,research_run_id,"
                "drafting_run_id,saved_path,failed_stage,created_at,updated_at FROM writing_tasks WHERE task_id=?",
                (str(task.task_id),),
            )
            return self._task(await cursor.fetchone())

    async def set_phase(self, attempt_id: UUID, phase: str) -> None:
        if phase not in {"retrieval", "generation", "validation", "publication"}:
            raise ValueError("unsupported execution phase")
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE writing_attempts SET phase=?,updated_at=? WHERE attempt_id=? AND status='running'",
                (phase, _now(), str(attempt_id)),
            )
            await db.commit()
            if cursor.rowcount != 1:
                raise WritingConflictError("attempt is not running")

    async def record_retrieval_result(
        self,
        attempt_id: UUID,
        *,
        retrieval_mode: str,
        fallback_reason: str | None,
    ) -> None:
        if retrieval_mode not in {"keyword", "vector", "hybrid"}:
            raise ValueError("unsupported retrieval mode")
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT config FROM writing_attempts WHERE attempt_id=? AND kind='research' AND status='running'",
                (str(attempt_id),),
            )
            row = await cursor.fetchone()
            if row is None:
                raise WritingConflictError("research attempt is not running")
            config = ExecutionConfig.model_validate_json(row[0]).model_copy(
                update={
                    "actual_retrieval_mode": retrieval_mode,
                    "retrieval_fallback_reason": fallback_reason,
                }
            )
            updated = await db.execute(
                "UPDATE writing_attempts SET config=?,updated_at=? WHERE attempt_id=? AND status='running'",
                (config.model_dump_json(), _now(), str(attempt_id)),
            )
            await db.commit()
            if updated.rowcount != 1:
                raise WritingConflictError("research attempt is not running")

    async def _load_attempt_task(self, db, attempt_id: UUID):
        cursor = await db.execute(
            "SELECT a.attempt_id,a.task_id,a.kind,a.input_version,a.run_id,a.status,a.phase,a.error_code,"
            "a.config,a.model_id,a.prompt_version,a.created_at,a.updated_at,"
            "t.task_id,t.session_id,t.topic,t.status,t.version,t.outline,t.draft,t.citations,"
            "t.research_run_id,t.drafting_run_id,t.saved_path,t.failed_stage,t.created_at,t.updated_at "
            "FROM writing_attempts a JOIN writing_tasks t ON t.task_id=a.task_id WHERE a.attempt_id=?",
            (str(attempt_id),),
        )
        row = await cursor.fetchone()
        if row is None:
            raise WritingNotFoundError(f"Writing attempt not found: {attempt_id}")
        attempt = self._attempt(row[:13])
        task = self._task(row[13:])
        run_cursor = await db.execute("SELECT session_id,status FROM runs WHERE id=?", (str(attempt.run_id),))
        run = await run_cursor.fetchone()
        if run is None or run[0] != str(task.session_id):
            raise WritingConflictError("attempt run is not linked to its task session")
        return attempt, task, run

    async def _mark_stale(self, db, attempt: ExecutionAttempt) -> None:
        timestamp = _now()
        await db.execute(
            "UPDATE writing_attempts SET status='conflicted',error_code='stale_result',updated_at=? "
            "WHERE attempt_id=? AND status='running'", (timestamp, str(attempt.attempt_id)),
        )
        await db.execute(
            "UPDATE runs SET status='failed',updated_at=? WHERE id=? AND status='running'",
            (timestamp, str(attempt.run_id)),
        )

    async def complete_attempt(
        self, attempt_id: UUID, *, outline: GeneratedOutline, citations,
    ) -> WritingTask:
        conflict = False
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            attempt, task, run = await self._load_attempt_task(db, attempt_id)
            if attempt.status != "running" or run[1] != "running":
                await db.rollback()
                raise WritingConflictError("attempt or run is not running")
            if task.status != WritingStatus.RESEARCHING or task.version != attempt.input_version:
                await self._mark_stale(db, attempt)
                await db.commit()
                conflict = True
            else:
                timestamp = _now()
                cursor = await db.execute(
                    "UPDATE writing_tasks SET status=?,version=?,outline=?,citations=?,research_run_id=?,"
                    "failed_stage=NULL,updated_at=? WHERE task_id=? AND status=? AND version=?",
                    (WritingStatus.AWAITING_OUTLINE_CONFIRMATION.value, attempt.input_version + 1,
                     outline.model_dump_json(),
                     json.dumps([item.model_dump(mode="json") for item in citations], ensure_ascii=False),
                     str(attempt.run_id), timestamp, str(task.task_id), WritingStatus.RESEARCHING.value,
                     attempt.input_version),
                )
                if cursor.rowcount != 1:
                    await self._mark_stale(db, attempt)
                    await db.commit()
                    conflict = True
                else:
                    await db.execute(
                        "UPDATE writing_attempts SET status='completed',phase='publication',updated_at=? "
                        "WHERE attempt_id=? AND status='running'", (timestamp, str(attempt_id)),
                    )
                    await db.execute(
                        "UPDATE runs SET status='completed',state=?,updated_at=? WHERE id=? AND status='running'",
                        (json.dumps({"task_id": str(task.task_id), "attempt_id": str(attempt_id),
                                     "phase": "publication", "result": "outline_published"}),
                         timestamp, str(attempt.run_id)),
                    )
                    await db.commit()
        if conflict:
            raise WritingConflictError("writing task changed before result publication")
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT task_id,session_id,topic,status,version,outline,draft,citations,research_run_id,"
                "drafting_run_id,saved_path,failed_stage,created_at,updated_at FROM writing_tasks WHERE task_id=?",
                (str(task.task_id),),
            )
            return self._task(await cursor.fetchone())

    async def fail_attempt(self, attempt_id: UUID, *, error_code: str) -> WritingTask:
        conflict = False
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            attempt, task, run = await self._load_attempt_task(db, attempt_id)
            if attempt.status != "running" or run[1] != "running":
                await db.rollback()
                raise WritingConflictError("attempt or run is not running")
            expected_status = (
                WritingStatus.DRAFTING if attempt.kind == "draft" else WritingStatus.RESEARCHING
            )
            failed_stage = expected_status.value
            if task.status != expected_status or task.version != attempt.input_version:
                await self._mark_stale(db, attempt)
                await db.commit()
                conflict = True
            else:
                timestamp = _now()
                cursor = await db.execute(
                    "UPDATE writing_tasks SET status='failed',version=?,failed_stage=?,updated_at=? "
                    "WHERE task_id=? AND status=? AND version=?",
                    (attempt.input_version + 1, failed_stage, timestamp, str(task.task_id), expected_status.value, attempt.input_version),
                )
                if cursor.rowcount != 1:
                    await self._mark_stale(db, attempt)
                    await db.commit()
                    conflict = True
                else:
                    await db.execute(
                        "UPDATE writing_attempts SET status='failed',error_code=?,updated_at=? "
                        "WHERE attempt_id=? AND status='running'", (error_code, timestamp, str(attempt_id)),
                    )
                    await db.execute(
                        "UPDATE runs SET status='failed',state=?,updated_at=? WHERE id=? AND status='running'",
                        (json.dumps({"task_id": str(task.task_id), "attempt_id": str(attempt_id),
                                     "phase": attempt.phase, "error_code": error_code}),
                         timestamp, str(attempt.run_id)),
                    )
                    await db.commit()
        if conflict:
            raise WritingConflictError("writing task changed before failure publication")
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT task_id,session_id,topic,status,version,outline,draft,citations,research_run_id,"
                "drafting_run_id,saved_path,failed_stage,created_at,updated_at FROM writing_tasks WHERE task_id=?",
                (str(task.task_id),),
            )
            return self._task(await cursor.fetchone())