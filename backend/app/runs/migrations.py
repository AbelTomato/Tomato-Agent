import aiosqlite


RUN_SCHEMA = """
    id TEXT PRIMARY KEY, task_type TEXT NOT NULL, request TEXT NOT NULL,
    workspace_id TEXT, status TEXT NOT NULL, error TEXT,
    version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    session_id TEXT, client_request_id TEXT, domain_run_id TEXT, result TEXT,
    stage TEXT, pending_action TEXT, request_digest TEXT
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
        if await (await db.execute("PRAGMA foreign_key_check")).fetchall():
            raise RuntimeError("run migration foreign key check failed")
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.execute("PRAGMA foreign_keys=ON")