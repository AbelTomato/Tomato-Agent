from pathlib import Path

import aiosqlite


class ProjectRepository:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    async def init(self):
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS desktop_projects (
                    project_id TEXT PRIMARY KEY,
                    environment_id TEXT NOT NULL,
                    service_instance_id TEXT NOT NULL,
                    root_path TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    device INTEGER NOT NULL,
                    inode INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(environment_id, service_instance_id, root_path)
                )
            """)
            await db.commit()

    async def get_for_path(self, environment_id: str, instance_id: str, path: str):
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute(
                "SELECT project_id, workspace_id, display_name, root_path, environment_id, "
                "service_instance_id, created_at, updated_at, device, inode FROM desktop_projects "
                "WHERE environment_id=? AND service_instance_id=? AND root_path=?",
                (environment_id, instance_id, path))
            row = await cursor.fetchone()
        return self._record(row) if row else None

    async def create(self, record: dict):
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("""INSERT INTO desktop_projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record["project_id"], record["environment_id"], record["service_instance_id"],
                 record["root_path"], record["workspace_id"], record["display_name"],
                 record["device"], record["inode"], record["created_at"], record["updated_at"]))
            await db.commit()
        return record

    async def delete_for_path(self, environment_id: str, instance_id: str, path: str):
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("DELETE FROM desktop_projects WHERE environment_id=? AND service_instance_id=? AND root_path=?",
                             (environment_id, instance_id, path))
            await db.commit()

    async def list_for_identity(self, environment_id: str, instance_id: str):
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute(
                "SELECT project_id, workspace_id, display_name, root_path, environment_id, "
                "service_instance_id, created_at, updated_at, device, inode FROM desktop_projects "
                "WHERE environment_id=? AND service_instance_id=? ORDER BY display_name, root_path",
                (environment_id, instance_id))
            rows = await cursor.fetchall()
        return [self._record(row) for row in rows]

    @staticmethod
    def _record(row):
        keys = ("project_id", "workspace_id", "display_name", "root_path", "environment_id",
                "service_instance_id", "created_at", "updated_at", "device", "inode")
        return dict(zip(keys, row))
