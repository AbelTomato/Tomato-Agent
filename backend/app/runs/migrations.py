import aiosqlite


RUN_SCHEMA = """
    id TEXT PRIMARY KEY, task_type TEXT NOT NULL, request TEXT NOT NULL,
    workspace_id TEXT, status TEXT NOT NULL, error TEXT,
    version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    session_id TEXT, client_request_id TEXT, domain_run_id TEXT, result TEXT,
    stage TEXT, pending_action TEXT, request_digest TEXT
"""


MODEL_CALL_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_model_calls (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES task_runs(id),
    lease_attempt_id TEXT NOT NULL REFERENCES execution_attempts(id),
    scope TEXT NOT NULL CHECK(scope IN ('top','domain')), step_id TEXT,
    operation_id TEXT, phase TEXT NOT NULL, attempt_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL, input_digest TEXT NOT NULL, config_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('reserved','committed','failed','unknown')),
    timeout_seconds REAL NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
    response TEXT, error_code TEXT, UNIQUE(run_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_model_calls_step ON execution_model_calls(run_id, scope, step_id);
INSERT OR IGNORE INTO execution_schema_migrations VALUES (2);
"""


async def migrate_unified_runs(db: aiosqlite.Connection) -> None:
    """Replace the legacy parent atomically without renaming child FK targets."""
    await db.execute("PRAGMA foreign_keys=OFF")
    try:
        await db.execute("BEGIN IMMEDIATE")
        columns = await (await db.execute("PRAGMA table_info(task_runs)")).fetchall()
        names = {row[1] for row in columns}
        if "session_id" not in names or next(row for row in columns if row[1] == "workspace_id")[3]:
            indexes = await (await db.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='task_runs' AND sql IS NOT NULL"
            )).fetchall()
            await db.execute(f"CREATE TABLE task_runs_unified ({RUN_SCHEMA})")
            copied = ", ".join('"' + row[1].replace('"', '""') + '"' for row in columns)
            await db.execute(f"INSERT INTO task_runs_unified ({copied}) SELECT {copied} FROM task_runs")
            await db.execute("DROP TABLE task_runs")
            await db.execute("ALTER TABLE task_runs_unified RENAME TO task_runs")
            for (statement,) in indexes:
                await db.execute(statement)
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_task_runs_request ON task_runs(session_id, client_request_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_task_runs_session ON task_runs(session_id, created_at, id)")
        await db.execute("CREATE TABLE IF NOT EXISTS session_task_queue (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
            "id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL REFERENCES sessions(id), client_request_id TEXT NOT NULL, "
            "request TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', run_id TEXT, "
            "created_at TEXT NOT NULL, UNIQUE(session_id, client_request_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS session_queue_control (session_id TEXT PRIMARY KEY REFERENCES sessions(id), "
            "paused INTEGER NOT NULL DEFAULT 0, acknowledged_run_id TEXT)")
        if await (await db.execute("PRAGMA foreign_key_check")).fetchall():
            raise RuntimeError("run migration foreign key check failed")
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.execute("PRAGMA foreign_keys=ON")