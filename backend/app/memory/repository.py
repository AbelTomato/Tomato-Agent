from datetime import datetime, timezone
import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import aiosqlite

from app.memory.models import MemoryItemCreate, MemoryItemView, MemorySourceInput


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryConflictError(Exception):
    pass


class MemoryRepository:
    SCHEMA_VERSION = 6

    def __init__(self, path: Path):
        self.path = path
        self.keyword_recall_semaphore = asyncio.Semaphore(1)
        self.cached_vector_recall_semaphore = asyncio.Semaphore(1)
        self.embedding_backfill_semaphores: dict[str, asyncio.Semaphore] = {}

    def embedding_backfill_semaphore(self, model_key: str) -> asyncio.Semaphore:
        return self.embedding_backfill_semaphores.setdefault(model_key, asyncio.Semaphore(1))

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS memory_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_meta (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_items (
                    item_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('preference','habit','background','episodic')),
                    scope TEXT NOT NULL CHECK (scope IN ('personal','project','session')),
                    scope_id TEXT NOT NULL,
                    topic_key TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('active','superseded','forgotten')),
                    confirmation TEXT NOT NULL CHECK (confirmation IN ('explicit','automatic','accepted')),
                    current_version INTEGER NOT NULL CHECK (current_version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_items_scope_status
                    ON memory_items(scope, scope_id, status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_memory_items_topic
                    ON memory_items(scope, scope_id, topic_key, status);
                CREATE TABLE IF NOT EXISTS memory_versions (
                    item_id TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK (version >= 1),
                    content TEXT NOT NULL,
                    previous_version INTEGER,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (item_id, version),
                    FOREIGN KEY (item_id) REFERENCES memory_items(item_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS memory_embeddings (
                    item_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    model_key TEXT NOT NULL,
                    dimensions INTEGER NOT NULL CHECK (dimensions > 0),
                    vector_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (item_id, version, model_key),
                    FOREIGN KEY (item_id, version)
                        REFERENCES memory_versions(item_id, version) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_memory_embeddings_model
                    ON memory_embeddings(model_key, item_id, version);
                CREATE INDEX IF NOT EXISTS idx_memory_embeddings_item_version
                    ON memory_embeddings(item_id, version, model_key);
                CREATE TABLE IF NOT EXISTS memory_contexts (
                    context_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    query_hash TEXT NOT NULL,
                    retrieval_mode TEXT NOT NULL,
                    used_chars INTEGER NOT NULL CHECK (used_chars >= 0),
                    used_tokens INTEGER NOT NULL CHECK (used_tokens >= 0),
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, step_id)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_contexts_scope_created
                    ON memory_contexts(scope_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS memory_context_items (
                    context_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    selection_reason TEXT NOT NULL,
                    rank INTEGER NOT NULL CHECK (rank >= 1),
                    used_chars INTEGER NOT NULL CHECK (used_chars >= 0),
                    used_tokens INTEGER NOT NULL CHECK (used_tokens >= 0),
                    source_available INTEGER NOT NULL CHECK (source_available IN (0,1)),
                    PRIMARY KEY(context_id, item_id, version, selection_reason),
                    FOREIGN KEY(context_id) REFERENCES memory_contexts(context_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_memory_context_items_fact
                    ON memory_context_items(item_id, version);
                CREATE TABLE IF NOT EXISTS memory_sources (
                    source_id TEXT PRIMARY KEY,
                    source_system TEXT NOT NULL,
                    source_installation_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    source_revision INTEGER NOT NULL CHECK (source_revision >= 1),
                    role TEXT NOT NULL,
                    message_at TEXT,
                    excerpt TEXT,
                    locator TEXT,
                    status TEXT NOT NULL CHECK (status IN ('active','deleted','revised')),
                    UNIQUE(source_system, source_installation_id, session_id, message_id, source_revision)
                );
                CREATE TABLE IF NOT EXISTS memory_evidence (
                    item_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    source_id TEXT NOT NULL,
                    evidence_role TEXT NOT NULL,
                    PRIMARY KEY(item_id, version, source_id, evidence_role),
                    FOREIGN KEY(item_id, version) REFERENCES memory_versions(item_id, version) ON DELETE CASCADE,
                    FOREIGN KEY(source_id) REFERENCES memory_sources(source_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS memory_write_requests (
                    request_id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    item_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_suppressions (
                    suppression_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    topic_key TEXT NOT NULL,
                    source_key_hash TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(scope, scope_id, topic_key, source_key_hash)
                );
                CREATE TABLE IF NOT EXISTS memory_jobs (
                    job_id TEXT PRIMARY KEY,
                    source_key TEXT NOT NULL UNIQUE,
                    scope_id TEXT NOT NULL,
                    payload TEXT,
                    status TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed','cancelled')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_jobs_ready
                    ON memory_jobs(status, retry_at, created_at);
                CREATE TABLE IF NOT EXISTS memory_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    scope_id TEXT NOT NULL,
                    topic_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending','accepted','rejected')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_candidates_status
                    ON memory_candidates(scope_id, status, created_at);
                CREATE TABLE IF NOT EXISTS memory_projections (
                    projection_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL CHECK (scope IN ('personal','project','session')),
                    scope_id TEXT NOT NULL,
                    projection_type TEXT NOT NULL,
                    projection_key TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('active','stale')),
                    generation INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(scope, scope_id, projection_type, projection_key)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_projections_scope
                    ON memory_projections(scope, scope_id, status);
                CREATE TABLE IF NOT EXISTS memory_projection_dependencies (
                    projection_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    PRIMARY KEY(projection_id, item_id, version),
                    FOREIGN KEY(projection_id) REFERENCES memory_projections(projection_id) ON DELETE CASCADE,
                    FOREIGN KEY(item_id, version) REFERENCES memory_versions(item_id, version) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS memory_projection_jobs (
                    job_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL CHECK (scope IN ('personal','project','session')),
                    scope_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(scope, scope_id)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_projection_jobs_ready
                    ON memory_projection_jobs(status, retry_at, created_at);
                CREATE TRIGGER IF NOT EXISTS invalidate_memory_projection_on_item_change
                AFTER UPDATE OF current_version, status ON memory_items
                WHEN NEW.status <> 'active' OR NEW.current_version <> OLD.current_version
                BEGIN
                    UPDATE memory_projections
                    SET content='', status='stale', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    WHERE projection_id IN (
                        SELECT projection_id FROM memory_projection_dependencies
                        WHERE item_id=OLD.item_id AND version=OLD.current_version
                    );
                END;
                CREATE TRIGGER IF NOT EXISTS invalidate_memory_projection_on_version_delete
                BEFORE DELETE ON memory_versions
                BEGIN
                    UPDATE memory_projections
                    SET content='', status='stale', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    WHERE projection_id IN (
                        SELECT projection_id FROM memory_projection_dependencies
                        WHERE item_id=OLD.item_id AND version=OLD.version
                    );
                END;
                INSERT OR IGNORE INTO memory_meta(key, value) VALUES ('generation', 0);
                INSERT OR IGNORE INTO memory_schema_migrations(version, applied_at)
                    VALUES (2, strftime('%Y-%m-%dT%H:%M:%fZ','now'));
                INSERT OR IGNORE INTO memory_schema_migrations(version, applied_at)
                    VALUES (3, strftime('%Y-%m-%dT%H:%M:%fZ','now'));
                INSERT OR IGNORE INTO memory_schema_migrations(version, applied_at)
                    VALUES (4, strftime('%Y-%m-%dT%H:%M:%fZ','now'));
                INSERT OR IGNORE INTO memory_schema_migrations(version, applied_at)
                    VALUES (5, strftime('%Y-%m-%dT%H:%M:%fZ','now'));
                INSERT OR IGNORE INTO memory_schema_migrations(version, applied_at)
                    VALUES (6, strftime('%Y-%m-%dT%H:%M:%fZ','now'));
                COMMIT;
                """
            )
            await db.execute(
                "UPDATE memory_jobs SET status='queued',retry_at=?,last_error='worker_restarted',updated_at=? "
                "WHERE status='running'",
                (_now(), _now()),
            )
            await db.execute(
                "UPDATE memory_projection_jobs SET status='queued',retry_at=?,last_error='worker_restarted',updated_at=? "
                "WHERE status='running'",
                (_now(), _now()),
            )
            await db.commit()

    async def enqueue_turn(self, *, scope_id: str, value: dict) -> dict:
        users = [message for message in value["messages"] if message["role"] == "user"]
        if not users:
            return {"status": "ignored", "reason": "no_user_messages"}
        payload = {**value, "messages": users}
        source_identity = {
            "source_system": value["source_system"],
            "source_installation_id": value["source_installation_id"],
            "session_id": value["session_id"],
            "messages": [(item["message_id"], item["revision"]) for item in users],
        }
        source_key = self._payload_hash(source_identity)
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            existing = await (await db.execute(
                "SELECT job_id, status FROM memory_jobs WHERE source_key = ?", (source_key,),
            )).fetchone()
            if existing:
                await db.commit()
                return {"job_id": existing[0], "status": existing[1], "duplicate": True}
            job_id = str(uuid4())
            await db.execute(
                "INSERT INTO memory_jobs(job_id,source_key,scope_id,payload,status,retry_at,created_at,updated_at) "
                "VALUES(?,?,?,?,'queued',?,?,?)",
                (job_id, source_key, scope_id, json.dumps(payload, ensure_ascii=False), now, now, now),
            )
            await db.commit()
        return {"job_id": job_id, "status": "queued", "duplicate": False}

    async def claim_job(self) -> dict | None:
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute(
                "SELECT job_id,scope_id,payload,attempts FROM memory_jobs "
                "WHERE status='queued' AND retry_at<=? ORDER BY created_at LIMIT 1", (now,),
            )).fetchone()
            if row is None:
                await db.commit()
                return None
            await db.execute(
                "UPDATE memory_jobs SET status='running',attempts=attempts+1,updated_at=? WHERE job_id=?",
                (now, row[0]),
            )
            await db.commit()
        return {"job_id": row[0], "scope_id": row[1], "payload": json.loads(row[2]),
                "attempts": row[3] + 1}

    async def finish_job(self, job_id: str, *, error: str | None = None, retry: bool = False) -> None:
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            if error is None:
                await db.execute(
                    "UPDATE memory_jobs SET status='completed',payload=NULL,last_error=NULL,updated_at=? "
                    "WHERE job_id=?", (now, job_id),
                )
            elif retry:
                row = await (await db.execute(
                    "SELECT attempts FROM memory_jobs WHERE job_id=?", (job_id,),
                )).fetchone()
                delay = min(60, 2 ** max(0, (row[0] if row else 1) - 1))
                from datetime import timedelta
                retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                await db.execute(
                    "UPDATE memory_jobs SET status='queued',retry_at=?,last_error=?,updated_at=? WHERE job_id=?",
                    (retry_at, error[:120], now, job_id),
                )
            else:
                await db.execute(
                    "UPDATE memory_jobs SET status='failed',payload=NULL,last_error=?,updated_at=? WHERE job_id=?",
                    (error[:120], now, job_id),
                )
            await db.commit()

    async def is_source_deleted(self, payload: dict) -> bool:
        async with aiosqlite.connect(self.path) as db:
            for message in payload["messages"]:
                row = await (await db.execute(
                    "SELECT status FROM memory_sources WHERE source_system=? AND source_installation_id=? "
                    "AND session_id=? AND message_id=? AND source_revision=?",
                    (payload["source_system"], payload["source_installation_id"], payload["session_id"],
                     message["message_id"], message["revision"]),
                )).fetchone()
                if row and row[0] == "deleted":
                    return True
        return False

    async def active_for_topic(self, scope_id: str, topic_key: str) -> list[MemoryItemView]:
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute(
                "SELECT item_id,kind,scope,scope_id,topic_key,status,confirmation,current_version,created_at,updated_at "
                "FROM memory_items WHERE scope='personal' AND scope_id=? AND topic_key=? AND status='active'",
                (scope_id, topic_key),
            )).fetchall()
            result = []
            for row in rows:
                content = await (await db.execute(
                    "SELECT content FROM memory_versions WHERE item_id=? AND version=?", (row[0], row[7]),
                )).fetchone()
                if content:
                    result.append(self._view(row, content))
            return result

    async def add_candidate(self, *, scope_id: str, payload: dict) -> str:
        candidate_id = str(uuid4())
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO memory_candidates VALUES (?,?,?,?,'pending',?,?)",
                (candidate_id, scope_id, payload["topic_key"], json.dumps(payload, ensure_ascii=False), now, now),
            )
            await db.commit()
        return candidate_id

    async def list_candidates(self, *, scope_id: str) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute(
                "SELECT candidate_id,payload,status,created_at FROM memory_candidates "
                "WHERE scope_id=? AND status='pending' ORDER BY created_at", (scope_id,),
            )).fetchall()
        return [{"candidate_id": row[0], **json.loads(row[1]), "status": row[2], "created_at": row[3]}
                for row in rows]

    async def decide_candidate(self, candidate_id: str, *, scope_id: str, action: str) -> dict:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute(
                "SELECT payload,status FROM memory_candidates WHERE candidate_id=? AND scope_id=?",
                (candidate_id, scope_id),
            )).fetchone()
            if row is None or row[1] != "pending":
                await db.rollback()
                raise KeyError("candidate_not_found")
            payload = json.loads(row[0])
            if action == "reject":
                await db.execute(
                    "UPDATE memory_candidates SET status='rejected',payload='{}',updated_at=? WHERE candidate_id=?",
                    (_now(), candidate_id),
                )
                await db.commit()
                return {"candidate_id": candidate_id, "status": "rejected"}
        sources = [MemorySourceInput.model_validate(source) for source in payload["sources"]]
        item = MemoryItemCreate(kind=payload["kind"], scope="personal", scope_id=scope_id,
                                topic_key=payload["topic_key"], content=payload["content"],
                                confirmation="accepted", sources=sources)
        conflict_ids = payload.get("conflicting_item_ids", [])
        created = await self.create(item, request_id=f"candidate:{candidate_id}",
                                    supersede_topic=bool(conflict_ids))
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE memory_candidates SET status='accepted',payload='{}',updated_at=? "
                "WHERE candidate_id=? AND status='pending'", (_now(), candidate_id),
            )
            await db.commit()
        return {"candidate_id": candidate_id, "status": "accepted", "item": created.model_dump()}

    @staticmethod
    def _payload_hash(value: dict) -> str:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _view(item_row, version_row) -> MemoryItemView:
        return MemoryItemView(
            item_id=item_row[0], kind=item_row[1], scope=item_row[2], scope_id=item_row[3],
            topic_key=item_row[4], status=item_row[5], confirmation=item_row[6],
            version=item_row[7], created_at=item_row[8], updated_at=item_row[9],
            content=version_row[0],
        )

    async def create(self, value: MemoryItemCreate, *, request_id: str,
                     supersede_topic: bool = False) -> MemoryItemView:
        if not request_id or len(request_id) > 160:
            raise ValueError("invalid_request_id")
        payload_hash = self._payload_hash(
            value.model_dump() if not supersede_topic else
            {"item": value.model_dump(), "supersede_topic": True}
        )
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            existing = await (await db.execute(
                "SELECT item_id, operation, payload_hash FROM memory_write_requests WHERE request_id = ?",
                (request_id,),
            )).fetchone()
            if existing:
                if existing[1] != "create" or existing[2] != payload_hash:
                    await db.rollback()
                    raise ValueError("idempotency_key_reused")
                result = await self.get(existing[0], db=db)
                await db.commit()
                if result is None:
                    raise RuntimeError("memory_idempotency_record_corrupt")
                return result
            if value.confirmation == "automatic":
                for source in value.sources:
                    source_identity = {key: getattr(source, key) for key in (
                        "source_system", "source_installation_id", "session_id", "message_id",
                        "source_revision",
                    )}
                    source_hash = self._payload_hash(source_identity)
                    suppressed = await (await db.execute(
                        "SELECT 1 FROM memory_suppressions WHERE scope = ? AND scope_id = ? "
                        "AND topic_key = ? AND source_key_hash IN ('', ?) LIMIT 1",
                        (value.scope, value.scope_id, value.topic_key, source_hash),
                    )).fetchone()
                    if suppressed:
                        await db.rollback()
                        raise ValueError("memory_suppressed")
            item_id = str(uuid4())
            timestamp = _now()
            await db.execute(
                "INSERT INTO memory_items VALUES (?, ?, ?, ?, ?, 'active', ?, 1, ?, ?)",
                (item_id, value.kind, value.scope, value.scope_id, value.topic_key,
                 value.confirmation, timestamp, timestamp),
            )
            await db.execute(
                "INSERT INTO memory_versions VALUES (?, 1, ?, NULL, 'created', ?)",
                (item_id, value.content, timestamp),
            )
            for source in value.sources:
                source_values = source.model_dump()
                source_id = str(uuid4())
                await db.execute(
                    "INSERT OR IGNORE INTO memory_sources(source_id, source_system, "
                    "source_installation_id, session_id, message_id, source_revision, role, "
                    "message_at, excerpt, locator, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
                    (source_id, source_values["source_system"], source_values["source_installation_id"],
                     source_values["session_id"], source_values["message_id"],
                     source_values["source_revision"], source_values["role"],
                     source_values["message_at"], source_values["excerpt"], source_values["locator"]),
                )
                source_row = await (await db.execute(
                    "SELECT source_id, status FROM memory_sources WHERE source_system = ? "
                    "AND source_installation_id = ? AND session_id = ? AND message_id = ? "
                    "AND source_revision = ?",
                    tuple(source_values[key] for key in ("source_system", "source_installation_id",
                          "session_id", "message_id", "source_revision")),
                )).fetchone()
                if source_row[1] == "deleted":
                    if value.confirmation == "automatic":
                        await db.rollback()
                        raise ValueError("memory_suppressed")
                    await db.execute(
                        "UPDATE memory_sources SET status='active', role=?, message_at=?, excerpt=?, locator=? "
                        "WHERE source_id=?",
                        (source_values["role"], source_values["message_at"], source_values["excerpt"],
                         source_values["locator"], source_row[0]),
                    )
                await db.execute(
                    "INSERT OR IGNORE INTO memory_evidence VALUES (?, 1, ?, 'support')",
                    (item_id, source_row[0]),
                )
            await db.execute(
                "INSERT INTO memory_write_requests VALUES (?, 'create', ?, ?)",
                (request_id, payload_hash, item_id),
            )
            await db.execute("UPDATE memory_meta SET value = value + 1 WHERE key = 'generation'")
            await self._queue_projection_refresh(db, scope=value.scope, scope_id=value.scope_id)
            if value.confirmation == "explicit":
                await db.execute(
                    "DELETE FROM memory_suppressions WHERE scope = ? AND scope_id = ? AND topic_key = ?",
                    (value.scope, value.scope_id, value.topic_key),
                )
            if supersede_topic:
                await db.execute(
                    "DELETE FROM memory_embeddings WHERE item_id IN ("
                    "SELECT item_id FROM memory_items WHERE scope=? AND scope_id=? AND topic_key=? "
                    "AND status='active' AND item_id<>?)",
                    (value.scope, value.scope_id, value.topic_key, item_id),
                )
                await db.execute(
                    "UPDATE memory_items SET status='superseded',updated_at=? "
                    "WHERE scope=? AND scope_id=? AND topic_key=? AND status='active' AND item_id<>?",
                    (timestamp, value.scope, value.scope_id, value.topic_key, item_id),
                )
            result = await self.get(item_id, db=db)
            await db.commit()
        assert result is not None
        return result

    async def get(self, item_id: str, *, db=None) -> MemoryItemView | None:
        if db is None:
            async with aiosqlite.connect(self.path) as connection:
                return await self.get(item_id, db=connection)
        item = await (await db.execute(
            "SELECT item_id, kind, scope, scope_id, topic_key, status, confirmation, "
            "current_version, created_at, updated_at FROM memory_items WHERE item_id = ?",
            (item_id,),
        )).fetchone()
        if item is None:
            return None
        if item[5] == "forgotten":
            return None
        version = await (await db.execute(
            "SELECT content FROM memory_versions WHERE item_id = ? AND version = ?",
            (item_id, item[7]),
        )).fetchone()
        if version is None:
            raise RuntimeError("memory_current_version_missing")
        return self._view(item, version)

    async def get_version(self, item_id: str, version: int) -> MemoryItemView | None:
        async with aiosqlite.connect(self.path) as db:
            item = await (await db.execute(
                "SELECT item_id,kind,scope,scope_id,topic_key,status,confirmation,current_version,created_at,updated_at "
                "FROM memory_items WHERE item_id=? AND status IN ('active','superseded')", (item_id,),
            )).fetchone()
            if item is None:
                return None
            content = await (await db.execute(
                "SELECT content FROM memory_versions WHERE item_id=? AND version=?", (item_id, version),
            )).fetchone()
            if content is None:
                return None
            row = list(item)
            row[7] = version
            return self._view(row, content)

    async def list_items(self, *, scope: str, scope_id: str, limit: int = 100,
                         offset: int = 0) -> list[MemoryItemView]:
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("invalid_pagination")
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute(
                "SELECT i.item_id, i.kind, i.scope, i.scope_id, i.topic_key, i.status, i.confirmation, "
                "i.current_version, i.created_at, i.updated_at, v.content FROM memory_items i "
                "LEFT JOIN memory_versions v ON v.item_id=i.item_id AND v.version=i.current_version "
                "WHERE i.scope = ? AND i.scope_id = ? AND i.status = 'active' "
                "ORDER BY i.updated_at DESC, i.item_id LIMIT ? OFFSET ?",
                (scope, scope_id, limit, offset),
            )).fetchall()
            result = []
            for row in rows:
                if row[10] is None:
                    raise RuntimeError("memory_current_version_missing")
                result.append(self._view(row[:10], (row[10],)))
            return result

    async def list_items_for_recall(self, *, scope: str, scope_id: str) -> list[MemoryItemView]:
        """Read a full active scope in one ordered query for recall ranking."""
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute(
                "SELECT i.item_id, i.kind, i.scope, i.scope_id, i.topic_key, i.status, i.confirmation, "
                "i.current_version, i.created_at, i.updated_at, v.content FROM memory_items i "
                "LEFT JOIN memory_versions v ON v.item_id=i.item_id AND v.version=i.current_version "
                "WHERE i.scope=? AND i.scope_id=? AND i.status='active' "
                "ORDER BY i.updated_at DESC,i.item_id",
                (scope, scope_id),
            )).fetchall()
        result = []
        for row in rows:
            if row[10] is None:
                raise RuntimeError("memory_current_version_missing")
            result.append(self._view(row[:10], (row[10],)))
        return result

    async def correct(self, item_id: str, *, content: str, reason: str,
                      expected_version: int, request_id: str) -> MemoryItemView:
        if not request_id or len(request_id) > 160:
            raise ValueError("invalid_request_id")
        payload_hash = self._payload_hash({
            "content": content, "reason": reason, "expected_version": expected_version,
            "item_id": item_id,
        })
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            existing = await (await db.execute(
                "SELECT item_id, operation, payload_hash FROM memory_write_requests WHERE request_id = ?",
                (request_id,),
            )).fetchone()
            if existing:
                if existing[1] != "correct" or existing[2] != payload_hash:
                    await db.rollback()
                    raise ValueError("idempotency_key_reused")
                result = await self.get(existing[0], db=db)
                await db.commit()
                if result is None:
                    raise RuntimeError("memory_idempotency_record_corrupt")
                return result
            item = await (await db.execute(
                "SELECT current_version, status, scope, scope_id FROM memory_items WHERE item_id = ?",
                (item_id,),
            )).fetchone()
            if item is None or item[1] != "active":
                await db.rollback()
                raise KeyError("memory_not_found")
            if item[0] != expected_version:
                await db.rollback()
                raise MemoryConflictError("memory_version_conflict")
            timestamp = _now()
            next_version = expected_version + 1
            await db.execute(
                "INSERT INTO memory_versions VALUES (?, ?, ?, ?, ?, ?)",
                (item_id, next_version, content, expected_version, reason, timestamp),
            )
            await db.execute(
                "INSERT OR IGNORE INTO memory_evidence(item_id, version, source_id, evidence_role) "
                "SELECT item_id, ?, source_id, evidence_role FROM memory_evidence "
                "WHERE item_id = ? AND version = ?",
                (next_version, item_id, expected_version),
            )
            await db.execute(
                "UPDATE memory_items SET current_version = ?, updated_at = ? WHERE item_id = ?",
                (next_version, timestamp, item_id),
            )
            await db.execute(
                "DELETE FROM memory_embeddings WHERE item_id=? AND version<>?",
                (item_id, next_version),
            )
            await db.execute(
                "INSERT INTO memory_write_requests VALUES (?, 'correct', ?, ?)",
                (request_id, payload_hash, item_id),
            )
            await db.execute("UPDATE memory_meta SET value = value + 1 WHERE key = 'generation'")
            await self._queue_projection_refresh(db, scope=item[2], scope_id=item[3])
            result = await self.get(item_id, db=db)
            await db.commit()
        assert result is not None
        return result

    async def generation(self) -> int:
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute(
                "SELECT value FROM memory_meta WHERE key = 'generation'"
            )).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _embedding_backfill_state_keys(model_key: str) -> tuple[str, str]:
        digest = hashlib.sha256(model_key.encode("utf-8")).hexdigest()
        prefix = f"embedding_backfill:{digest}"
        return f"{prefix}:failures", f"{prefix}:retry_at"

    async def embedding_backfill_state(self, *, model_key: str) -> dict:
        failure_key, retry_key = self._embedding_backfill_state_keys(model_key)
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute(
                "SELECT key,value FROM memory_meta WHERE key IN (?,?)", (failure_key, retry_key),
            )).fetchall()
        values = dict(rows)
        return {"failures": int(values.get(failure_key, 0)),
                "retry_at": int(values.get(retry_key, 0))}

    async def record_embedding_backfill_failure(self, *, model_key: str, failures: int,
                                                retry_at: int) -> None:
        if failures < 1 or retry_at < 0:
            raise ValueError("invalid_memory_embedding_backfill_state")
        failure_key, retry_key = self._embedding_backfill_state_keys(model_key)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.executemany(
                "INSERT INTO memory_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(failure_key, failures), (retry_key, retry_at)],
            )
            await db.commit()

    async def clear_embedding_backfill_failure(self, *, model_key: str) -> None:
        failure_key, retry_key = self._embedding_backfill_state_keys(model_key)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM memory_meta WHERE key IN (?,?)", (failure_key, retry_key),
            )
            await db.commit()

    async def save_projection(self, *, scope: str, scope_id: str, projection_type: str,
                              projection_key: str, content: str, expected_generation: int,
                              dependencies: list[dict]) -> dict:
        if (scope not in {"personal", "project", "session"} or not scope_id
                or not projection_type or len(projection_type) > 80
                or not projection_key or len(projection_key) > 200
                or not content or len(content) > 4000 or not dependencies):
            raise ValueError("invalid_memory_projection")
        unique_dependencies = {(item.get("item_id"), item.get("version")) for item in dependencies}
        if (len(unique_dependencies) != len(dependencies)
                or any(not item_id or not isinstance(version, int) or version < 1
                       for item_id, version in unique_dependencies)):
            raise ValueError("invalid_memory_projection_dependencies")

        timestamp = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            current_generation = int((await (await db.execute(
                "SELECT value FROM memory_meta WHERE key='generation'",
            )).fetchone())[0])
            if current_generation != expected_generation:
                await db.rollback()
                raise MemoryConflictError("memory_generation_conflict")
            for item_id, version in unique_dependencies:
                row = await (await db.execute(
                    "SELECT i.scope, i.scope_id, i.status, i.current_version, "
                    "EXISTS(SELECT 1 FROM memory_evidence e JOIN memory_sources s USING(source_id) "
                    "WHERE e.item_id=i.item_id AND e.version=? AND s.status='active') "
                    "FROM memory_items i WHERE i.item_id=?",
                    (version, item_id),
                )).fetchone()
                if (row is None or row[0] != scope or row[1] != scope_id or row[2] != "active"
                        or row[3] != version or not row[4]):
                    await db.rollback()
                    raise MemoryConflictError("memory_projection_dependency_stale")

            existing = await (await db.execute(
                "SELECT projection_id FROM memory_projections WHERE scope=? AND scope_id=? "
                "AND projection_type=? AND projection_key=?",
                (scope, scope_id, projection_type, projection_key),
            )).fetchone()
            projection_id = existing[0] if existing else str(uuid4())
            await db.execute(
                "INSERT INTO memory_projections(projection_id,scope,scope_id,projection_type,"
                "projection_key,content,status,generation,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'active',?,?,?) ON CONFLICT(scope,scope_id,projection_type,projection_key) "
                "DO UPDATE SET content=excluded.content,status='active',generation=excluded.generation,"
                "updated_at=excluded.updated_at",
                (projection_id, scope, scope_id, projection_type, projection_key, content,
                 current_generation, timestamp, timestamp),
            )
            await db.execute(
                "DELETE FROM memory_projection_dependencies WHERE projection_id=?", (projection_id,),
            )
            await db.executemany(
                "INSERT INTO memory_projection_dependencies(projection_id,item_id,version) VALUES(?,?,?)",
                [(projection_id, item_id, version) for item_id, version in sorted(unique_dependencies)],
            )
            await db.commit()
        return {
            "projection_id": projection_id, "scope": scope, "scope_id": scope_id,
            "projection_type": projection_type, "projection_key": projection_key,
            "content": content, "status": "active", "generation": current_generation,
            "dependencies": [{"item_id": item_id, "version": version}
                             for item_id, version in sorted(unique_dependencies)],
        }

    async def get_projection(self, *, scope: str, scope_id: str,
                             projection_type: str, projection_key: str) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute(
                "SELECT projection_id,content,status,generation,created_at,updated_at FROM memory_projections "
                "WHERE scope=? AND scope_id=? AND projection_type=? AND projection_key=?",
                (scope, scope_id, projection_type, projection_key),
            )).fetchone()
            if row is None or row[2] != "active":
                await db.commit()
                return None
            invalid = await (await db.execute(
                "SELECT count(*), sum(CASE WHEN i.status<>'active' OR i.current_version<>d.version "
                "OR NOT EXISTS(SELECT 1 FROM memory_evidence e JOIN memory_sources s USING(source_id) "
                "WHERE e.item_id=d.item_id AND e.version=d.version AND s.status='active') "
                "THEN 1 ELSE 0 END) "
                "FROM memory_projection_dependencies d JOIN memory_items i USING(item_id) "
                "WHERE d.projection_id=?", (row[0],),
            )).fetchone()
            if not invalid[0] or invalid[1]:
                await db.execute(
                    "UPDATE memory_projections SET content='',status='stale',updated_at=? WHERE projection_id=?",
                    (_now(), row[0]),
                )
                await db.commit()
                return None
            dependencies = await (await db.execute(
                "SELECT item_id,version FROM memory_projection_dependencies "
                "WHERE projection_id=? ORDER BY item_id,version", (row[0],),
            )).fetchall()
            await db.commit()
        return {
            "projection_id": row[0], "scope": scope, "scope_id": scope_id,
            "projection_type": projection_type, "projection_key": projection_key,
            "content": row[1], "status": row[2], "generation": row[3],
            "created_at": row[4], "updated_at": row[5],
            "dependencies": [{"item_id": item_id, "version": version}
                             for item_id, version in dependencies],
        }

    @staticmethod
    async def _queue_projection_refresh(db, *, scope: str, scope_id: str) -> None:
        now = _now()
        await db.execute(
            "INSERT INTO memory_projection_jobs(job_id,scope,scope_id,status,retry_at,created_at,updated_at) "
            "VALUES(?,?,?,'queued',?,?,?) ON CONFLICT(scope,scope_id) DO UPDATE SET "
            "status='queued',attempts=0,retry_at=excluded.retry_at,last_error=NULL,updated_at=excluded.updated_at",
            (str(uuid4()), scope, scope_id, now, now, now),
        )

    async def claim_projection_job(self) -> dict | None:
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute(
                "SELECT job_id,scope,scope_id,attempts FROM memory_projection_jobs "
                "WHERE status='queued' AND retry_at<=? ORDER BY created_at LIMIT 1", (now,),
            )).fetchone()
            if row is None:
                await db.commit()
                return None
            generation = int((await (await db.execute(
                "SELECT value FROM memory_meta WHERE key='generation'",
            )).fetchone())[0])
            await db.execute(
                "UPDATE memory_projection_jobs SET status='running',attempts=attempts+1,updated_at=? "
                "WHERE job_id=?", (now, row[0]),
            )
            await db.commit()
        return {"job_id": row[0], "scope": row[1], "scope_id": row[2],
                "attempts": row[3] + 1, "generation": generation}

    async def finish_projection_job(self, job_id: str, *, error: str | None = None,
                                    retry: bool = False) -> None:
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            if error is None:
                await db.execute(
                    "UPDATE memory_projection_jobs SET status='completed',last_error=NULL,updated_at=? "
                    "WHERE job_id=? AND status='running'", (now, job_id),
                )
            elif retry:
                row = await (await db.execute(
                    "SELECT attempts FROM memory_projection_jobs WHERE job_id=?", (job_id,),
                )).fetchone()
                from datetime import timedelta
                retry_at = (datetime.now(timezone.utc) + timedelta(
                    seconds=min(60, 2 ** max(0, (row[0] if row else 1) - 1)),
                )).isoformat()
                await db.execute(
                    "UPDATE memory_projection_jobs SET status='queued',retry_at=?,last_error=?,updated_at=? "
                    "WHERE job_id=? AND status='running'", (retry_at, error[:120], now, job_id),
                )
            else:
                await db.execute(
                    "UPDATE memory_projection_jobs SET status='failed',last_error=?,updated_at=? "
                    "WHERE job_id=? AND status='running'",
                    (error[:120], now, job_id),
                )
            await db.commit()

    async def embeddings_for(self, item_versions: list[tuple[str, int]], *, model_key: str) -> dict:
        if not item_versions:
            return {}
        result = {}
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "CREATE TEMP TABLE requested_item_versions (item_id TEXT NOT NULL, version INTEGER NOT NULL, "
                "PRIMARY KEY(item_id,version)) WITHOUT ROWID"
            )
            await db.executemany(
                "INSERT OR IGNORE INTO requested_item_versions(item_id,version) VALUES(?,?)",
                list(dict.fromkeys(item_versions)),
            )
            rows = await (await db.execute(
                "SELECT e.item_id,e.version,e.dimensions,e.vector_json FROM requested_item_versions r "
                "JOIN memory_embeddings e ON e.item_id=r.item_id AND e.version=r.version AND e.model_key=? "
                "JOIN memory_items i ON i.item_id=e.item_id "
                "WHERE i.status='active' AND i.current_version=e.version",
                (model_key,),
            )).fetchall()
            allowed_versions = dict(item_versions)
            for item_id, version, dimensions, vector_json in rows:
                if allowed_versions.get(item_id) != version:
                    continue
                try:
                    vector = json.loads(vector_json)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(vector, list) and len(vector) == dimensions:
                    result[item_id] = vector
        return result

    async def items_missing_embeddings(self, *, model_key: str, limit: int = 64) -> list[dict]:
        if not model_key or not 1 <= limit <= 256:
            raise ValueError("invalid_memory_embedding_backfill_request")
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute(
                "SELECT i.item_id,i.current_version,v.content,i.topic_key FROM memory_items i "
                "JOIN memory_versions v ON v.item_id=i.item_id AND v.version=i.current_version "
                "LEFT JOIN memory_embeddings e ON e.item_id=i.item_id AND e.version=i.current_version "
                "AND e.model_key=? WHERE i.status='active' AND e.item_id IS NULL "
                "ORDER BY i.updated_at,i.item_id LIMIT ?",
                (model_key, limit),
            )).fetchall()
        return [{"item_id": row[0], "version": row[1], "content": row[2], "topic_key": row[3]}
                for row in rows]

    async def store_embeddings(self, *, model_key: str, dimensions: int,
                               embeddings: list[tuple[str, int, list[float]]]) -> None:
        if not embeddings:
            return
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            for item_id, version, vector in embeddings:
                current = await (await db.execute(
                    "SELECT current_version,status FROM memory_items WHERE item_id=?", (item_id,),
                )).fetchone()
                if current != (version, "active"):
                    continue
                await db.execute(
                    "INSERT INTO memory_embeddings(item_id,version,model_key,dimensions,vector_json,updated_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(item_id,version,model_key) DO UPDATE SET "
                    "dimensions=excluded.dimensions,vector_json=excluded.vector_json,updated_at=excluded.updated_at",
                    (item_id, version, model_key, dimensions,
                     json.dumps(vector, separators=(",", ":"), allow_nan=False), now),
                )
            await db.commit()

    async def record_context(self, *, run_id: str, session_id: str, step_id: str,
                             scope_id: str, generation: int, query: str,
                             retrieval_mode: str, used_chars: int, used_tokens: int,
                             items: list[dict]) -> str | None:
        context_id = str(uuid4())
        now = _now()
        query_hash = self._payload_hash({"query": query})
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            current_generation = int((await (await db.execute(
                "SELECT value FROM memory_meta WHERE key='generation'",
            )).fetchone())[0])
            if current_generation != generation:
                await db.rollback()
                return None
            await db.execute(
                "INSERT OR IGNORE INTO memory_contexts(context_id,run_id,session_id,step_id,scope_id,"
                "generation,query_hash,retrieval_mode,used_chars,used_tokens,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (context_id, run_id, session_id, step_id, scope_id, generation, query_hash,
                 retrieval_mode, used_chars, used_tokens, now),
            )
            row = await (await db.execute(
                "SELECT context_id FROM memory_contexts WHERE run_id=? AND step_id=?",
                (run_id, step_id),
            )).fetchone()
            context_id = row[0]
            for item in items:
                await db.execute(
                    "INSERT OR IGNORE INTO memory_context_items(context_id,item_id,version,selection_reason,"
                    "rank,used_chars,used_tokens,source_available) VALUES(?,?,?,?,?,?,?,?)",
                    (context_id, item["item_id"], item["version"], item["selection_reason"], item["rank"],
                     item["used_chars"], item["used_tokens"], int(bool(item["source_available"]))),
                )
            await db.commit()
        return context_id

    async def list_contexts(self, *, run_id: str) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            contexts = await (await db.execute(
                "SELECT * FROM memory_contexts WHERE run_id=? ORDER BY created_at,step_id", (run_id,),
            )).fetchall()
            result = []
            for context in contexts:
                items = await (await db.execute(
                    "SELECT item_id,version,selection_reason,rank,used_chars,used_tokens,source_available "
                    "FROM memory_context_items WHERE context_id=? ORDER BY rank,item_id",
                    (context["context_id"],),
                )).fetchall()
                result.append({**dict(context), "items": [dict(item) for item in items]})
            return result

    async def sources(self, item_id: str, version: int) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute(
                "SELECT s.source_system, s.source_installation_id, s.session_id, s.message_id, "
                "s.source_revision, s.role, s.message_at, s.excerpt, s.locator, s.status, e.evidence_role "
                "FROM memory_evidence e JOIN memory_sources s USING(source_id) "
                "WHERE e.item_id = ? AND e.version = ? ORDER BY s.source_id",
                (item_id, version),
            )).fetchall()
        keys = ("source_system", "source_installation_id", "session_id", "message_id",
                "source_revision", "role", "message_at", "excerpt", "locator", "status", "evidence_role")
        return [dict(zip(keys, row)) for row in rows]

    async def sources_for_items(self, item_versions: list[tuple[str, int]]) -> dict[tuple[str, int], list[dict]]:
        if not item_versions:
            return {}
        keys = ("source_system", "source_installation_id", "session_id", "message_id",
                "source_revision", "role", "message_at", "excerpt", "locator", "status", "evidence_role")
        result: dict[tuple[str, int], list[dict]] = {}
        async with aiosqlite.connect(self.path) as db:
            for offset in range(0, len(item_versions), 300):
                batch = item_versions[offset:offset + 300]
                values = ",".join("(?,?)" for _ in batch)
                parameters = tuple(value for item in batch for value in item)
                rows = await (await db.execute(
                    "SELECT e.item_id, e.version, s.source_system, s.source_installation_id, "
                    "s.session_id, s.message_id, s.source_revision, s.role, s.message_at, "
                    "s.excerpt, s.locator, s.status, e.evidence_role "
                    "FROM memory_evidence e JOIN memory_sources s USING(source_id) "
                    "WHERE (e.item_id,e.version) IN (" + values + ") "
                    "ORDER BY e.item_id,e.version,s.source_id",
                    parameters,
                )).fetchall()
                for row in rows:
                    identity = (row[0], row[1])
                    result.setdefault(identity, []).append(dict(zip(keys, row[2:])))
        return result

    async def sources_for_recall(self, item_versions: list[tuple[str, int]]) -> dict[tuple[str, int], list[dict]]:
        """Load a large recall scope's evidence with one join instead of many IN batches."""
        if not item_versions:
            return {}
        keys = ("source_system", "source_installation_id", "session_id", "message_id",
                "source_revision", "role", "message_at", "excerpt", "locator", "status", "evidence_role")
        result: dict[tuple[str, int], list[dict]] = {}
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "CREATE TEMP TABLE recall_item_versions (item_id TEXT NOT NULL, version INTEGER NOT NULL, "
                "PRIMARY KEY(item_id,version)) WITHOUT ROWID"
            )
            await db.executemany(
                "INSERT INTO recall_item_versions(item_id,version) VALUES(?,?)",
                list(dict.fromkeys(item_versions)),
            )
            rows = await (await db.execute(
                "SELECT e.item_id,e.version,s.source_system,s.source_installation_id,s.session_id,"
                "s.message_id,s.source_revision,s.role,s.message_at,s.excerpt,s.locator,s.status,e.evidence_role "
                "FROM recall_item_versions r JOIN memory_evidence e "
                "ON e.item_id=r.item_id AND e.version=r.version "
                "JOIN memory_sources s USING(source_id) "
                "ORDER BY e.item_id,e.version,s.source_id"
            )).fetchall()
        for row in rows:
            identity = (row[0], row[1])
            result.setdefault(identity, []).append(dict(zip(keys, row[2:])))
        return result

    async def forget(self, item_id: str, *, expected_version: int, request_id: str) -> dict:
        if not request_id or len(request_id) > 160:
            raise ValueError("invalid_request_id")
        payload_hash = self._payload_hash({"item_id": item_id, "expected_version": expected_version})
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            existing = await (await db.execute(
                "SELECT item_id, operation, payload_hash FROM memory_write_requests WHERE request_id = ?",
                (request_id,),
            )).fetchone()
            if existing:
                if existing[1] != "forget" or existing[2] != payload_hash:
                    await db.rollback()
                    raise ValueError("idempotency_key_reused")
                await db.commit()
                return {"item_id": item_id, "status": "forgotten", "generation": await self.generation()}
            item = await (await db.execute(
                "SELECT scope, scope_id, topic_key, current_version, status "
                "FROM memory_items WHERE item_id = ?", (item_id,),
            )).fetchone()
            if item is None or item[4] != "active":
                await db.rollback()
                raise KeyError("memory_not_found")
            if item[3] != expected_version:
                await db.rollback()
                raise MemoryConflictError("memory_version_conflict")
            timestamp = _now()
            sources = await (await db.execute(
                "SELECT s.source_system, s.source_installation_id, s.session_id, s.message_id, "
                "s.source_revision, s.source_id FROM memory_evidence e JOIN memory_sources s USING(source_id) "
                "WHERE e.item_id = ?", (item_id,),
            )).fetchall()
            for source in sources:
                source_identity = dict(zip(("source_system", "source_installation_id", "session_id",
                                            "message_id", "source_revision"), source[:5]))
                key_hash = self._payload_hash(source_identity)
                await db.execute(
                    "INSERT OR IGNORE INTO memory_suppressions VALUES (?, ?, ?, ?, ?, ?)",
                    (str(uuid4()), item[0], item[1], item[2], key_hash, timestamp),
                )
            await db.execute(
                "INSERT OR IGNORE INTO memory_suppressions VALUES (?, ?, ?, ?, '', ?)",
                (str(uuid4()), item[0], item[1], item[2], timestamp),
            )
            await db.execute(
                "DELETE FROM memory_contexts WHERE context_id IN ("
                "SELECT context_id FROM memory_context_items WHERE item_id=?)", (item_id,),
            )
            await db.execute("DELETE FROM memory_versions WHERE item_id = ?", (item_id,))
            for source in sources:
                still_used = await (await db.execute(
                    "SELECT 1 FROM memory_evidence WHERE source_id = ? LIMIT 1", (source[5],)
                )).fetchone()
                if not still_used:
                    await db.execute(
                        "UPDATE memory_sources SET excerpt = NULL, locator = NULL WHERE source_id = ?",
                        (source[5],),
                    )
            await db.execute(
                "UPDATE memory_items SET status = 'forgotten', updated_at = ? WHERE item_id = ?",
                (timestamp, item_id),
            )
            await db.execute(
                "INSERT INTO memory_write_requests VALUES (?, 'forget', ?, ?)",
                (request_id, payload_hash, item_id),
            )
            await db.execute("UPDATE memory_meta SET value = value + 1 WHERE key = 'generation'")
            await self._queue_projection_refresh(db, scope=item[0], scope_id=item[1])
            generation = int((await (await db.execute(
                "SELECT value FROM memory_meta WHERE key = 'generation'"
            )).fetchone())[0])
            await db.commit()
        return {"item_id": item_id, "status": "forgotten", "generation": generation}

    async def delete_source(self, source_key: dict, *, request_id: str) -> dict:
        if not request_id or len(request_id) > 160:
            raise ValueError("invalid_request_id")
        required = ("source_system", "source_installation_id", "session_id", "message_id", "source_revision")
        if any(key not in source_key for key in required):
            raise ValueError("invalid_source_key")
        payload_hash = self._payload_hash(source_key)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            existing = await (await db.execute(
                "SELECT operation, payload_hash FROM memory_write_requests WHERE request_id = ?",
                (request_id,),
            )).fetchone()
            if existing:
                if existing != ("source_delete", payload_hash):
                    await db.rollback()
                    raise ValueError("idempotency_key_reused")
                await db.commit()
                return {"status": "deleted", "generation": await self.generation()}
            source = await (await db.execute(
                "SELECT source_id FROM memory_sources WHERE source_system = ? "
                "AND source_installation_id = ? AND session_id = ? AND message_id = ? "
                "AND source_revision = ?", tuple(source_key[key] for key in required),
            )).fetchone()
            await db.execute("DELETE FROM memory_contexts WHERE session_id=?", (source_key["session_id"],))
            if source:
                await db.execute(
                    "UPDATE memory_sources SET status='deleted', excerpt=NULL, locator=NULL "
                    "WHERE source_id=?",
                    (source[0],),
                )
                item_rows = await (await db.execute(
                    "SELECT DISTINCT i.item_id, i.scope, i.scope_id, i.topic_key "
                    "FROM memory_items i JOIN memory_evidence e USING(item_id) "
                    "WHERE e.source_id = ? AND i.status = 'active'", (source[0],),
                )).fetchall()
                await db.execute("DELETE FROM memory_evidence WHERE source_id = ?", (source[0],))
                for item_id, scope, scope_id, topic_key in item_rows:
                    supports = await (await db.execute(
                        "SELECT 1 FROM memory_evidence WHERE item_id = ? AND version = "
                        "(SELECT current_version FROM memory_items WHERE item_id = ?) LIMIT 1",
                        (item_id, item_id),
                    )).fetchone()
                    if not supports:
                        await db.execute(
                            "DELETE FROM memory_contexts WHERE context_id IN ("
                            "SELECT context_id FROM memory_context_items WHERE item_id=?)", (item_id,),
                        )
                        await db.execute("DELETE FROM memory_versions WHERE item_id = ?", (item_id,))
                        await db.execute(
                            "UPDATE memory_items SET status='forgotten', updated_at=? WHERE item_id=?",
                            (_now(), item_id),
                        )
                        await db.execute(
                            "INSERT OR IGNORE INTO memory_suppressions VALUES (?, ?, ?, ?, '', ?)",
                            (str(uuid4()), scope, scope_id, topic_key, _now()),
                        )
                for scope, scope_id in {(row[1], row[2]) for row in item_rows}:
                    await self._queue_projection_refresh(db, scope=scope, scope_id=scope_id)
            else:
                await db.execute(
                    "INSERT INTO memory_sources(source_id,source_system,source_installation_id,session_id,"
                    "message_id,source_revision,role,status) VALUES(?,?,?,?,?,?,'user','deleted')",
                    (str(uuid4()), *(source_key[key] for key in required)),
                )
            await db.execute(
                "INSERT INTO memory_write_requests VALUES (?, 'source_delete', ?, '')",
                (request_id, payload_hash),
            )
            await db.execute("UPDATE memory_meta SET value = value + 1 WHERE key = 'generation'")
            generation = int((await (await db.execute(
                "SELECT value FROM memory_meta WHERE key = 'generation'"
            )).fetchone())[0])
            await db.commit()
        return {"status": "deleted", "generation": generation}

    async def clear(self, *, scope: str, scope_id: str, expected_generation: int,
                    request_id: str) -> dict:
        if not request_id or len(request_id) > 160:
            raise ValueError("invalid_request_id")
        payload_hash = self._payload_hash({
            "scope": scope, "scope_id": scope_id,
            "expected_generation": expected_generation,
        })
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            existing = await (await db.execute(
                "SELECT operation, payload_hash, item_id FROM memory_write_requests WHERE request_id = ?",
                (request_id,),
            )).fetchone()
            if existing:
                if existing[0] != "clear" or existing[1] != payload_hash:
                    await db.rollback()
                    raise ValueError("idempotency_key_reused")
                current_generation = int((await (await db.execute(
                    "SELECT value FROM memory_meta WHERE key='generation'"
                )).fetchone())[0])
                await db.commit()
                return {"status": "cleared", "generation": current_generation}
            current_generation = int((await (await db.execute(
                "SELECT value FROM memory_meta WHERE key='generation'"
            )).fetchone())[0])
            if current_generation != expected_generation:
                await db.rollback()
                raise MemoryConflictError("memory_generation_conflict")
            rows = await (await db.execute(
                "SELECT item_id, topic_key, current_version FROM memory_items "
                "WHERE scope = ? AND scope_id = ? AND status = 'active'",
                (scope, scope_id),
            )).fetchall()
            timestamp = _now()
            source_ids: set[str] = set()
            for item_id, topic_key, version in rows:
                sources = await (await db.execute(
                    "SELECT s.source_system, s.source_installation_id, s.session_id, "
                    "s.message_id, s.source_revision, s.source_id "
                    "FROM memory_evidence e JOIN memory_sources s USING(source_id) "
                    "WHERE e.item_id = ? AND e.version = ?", (item_id, version),
                )).fetchall()
                for source in sources:
                    identity = dict(zip(("source_system", "source_installation_id", "session_id",
                                         "message_id", "source_revision"), source[:5]))
                    source_hash = self._payload_hash(identity)
                    source_ids.add(source[5])
                    await db.execute(
                        "INSERT OR IGNORE INTO memory_suppressions VALUES (?, ?, ?, ?, ?, ?)",
                        (str(uuid4()), scope, scope_id, topic_key, source_hash, timestamp),
                    )
                await db.execute(
                    "INSERT OR IGNORE INTO memory_suppressions VALUES (?, ?, ?, ?, '', ?)",
                    (str(uuid4()), scope, scope_id, topic_key, timestamp),
                )
                await db.execute(
                    "DELETE FROM memory_contexts WHERE context_id IN ("
                    "SELECT context_id FROM memory_context_items WHERE item_id=?)", (item_id,),
                )
                await db.execute("DELETE FROM memory_versions WHERE item_id = ?", (item_id,))
                await db.execute(
                    "UPDATE memory_items SET status='forgotten', updated_at=? WHERE item_id=?",
                    (timestamp, item_id),
                )
            for source_id in source_ids:
                still_used = await (await db.execute(
                    "SELECT 1 FROM memory_evidence WHERE source_id = ? LIMIT 1", (source_id,)
                )).fetchone()
                if not still_used:
                    await db.execute(
                        "UPDATE memory_sources SET excerpt=NULL, locator=NULL WHERE source_id=?",
                        (source_id,),
                    )
            await db.execute("UPDATE memory_meta SET value = value + 1 WHERE key='generation'")
            await self._queue_projection_refresh(db, scope=scope, scope_id=scope_id)
            generation = int((await (await db.execute(
                "SELECT value FROM memory_meta WHERE key='generation'"
            )).fetchone())[0])
            await db.execute(
                "INSERT INTO memory_write_requests VALUES (?, 'clear', ?, '')",
                (request_id, payload_hash),
            )
            await db.commit()
        return {"status": "cleared", "generation": generation, "forgotten_count": len(rows)}
