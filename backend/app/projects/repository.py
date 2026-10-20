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
            await db.execute("""
                CREATE TABLE IF NOT EXISTS desktop_project_metadata (
                    project_id TEXT PRIMARY KEY,
                    environment_id TEXT NOT NULL,
                    service_instance_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS project_directories (
                    project_id TEXT NOT NULL REFERENCES desktop_project_metadata(project_id) ON DELETE CASCADE,
                    root_path TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    device INTEGER NOT NULL,
                    inode INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, root_path)
                )
            """)
            await db.execute("""
                INSERT OR IGNORE INTO desktop_project_metadata
                    (project_id, environment_id, service_instance_id, display_name, created_at, updated_at)
                SELECT project_id, environment_id, service_instance_id, display_name, created_at, updated_at
                FROM desktop_projects
            """)
            await db.execute("""
                INSERT OR IGNORE INTO project_directories
                    (project_id, root_path, workspace_id, device, inode, created_at)
                SELECT project_id, root_path, workspace_id, device, inode, created_at FROM desktop_projects
            """)
            await db.commit()

    async def create_project(self, record: dict):
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("""INSERT INTO desktop_project_metadata
                VALUES (?, ?, ?, ?, ?, ?)""", (record["project_id"], record["environment_id"],
                record["service_instance_id"], record["display_name"], record["created_at"], record["updated_at"]))
            await db.commit()
        return record

    async def get_project(self, project_id: str, environment_id: str, instance_id: str):
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute("""SELECT project_id, environment_id, service_instance_id,
                display_name, created_at, updated_at FROM desktop_project_metadata
                WHERE project_id=? AND environment_id=? AND service_instance_id=?""",
                (project_id, environment_id, instance_id))
            row = await cursor.fetchone()
        if not row:
            return None
        return dict(zip(("project_id", "environment_id", "service_instance_id", "display_name", "created_at", "updated_at"), row))

    async def list_projects(self, environment_id: str, instance_id: str):
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute("""SELECT project_id, environment_id, service_instance_id,
                display_name, created_at, updated_at FROM desktop_project_metadata
                WHERE environment_id=? AND service_instance_id=? ORDER BY display_name, project_id""",
                (environment_id, instance_id))
            rows = await cursor.fetchall()
        keys = ("project_id", "environment_id", "service_instance_id", "display_name", "created_at", "updated_at")
        return [dict(zip(keys, row)) for row in rows]

    async def rename_project(self, project_id: str, environment_id: str, instance_id: str, name: str, updated_at: str):
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("""UPDATE desktop_projects SET display_name=?, updated_at=?
                WHERE project_id=? AND environment_id=? AND service_instance_id=?""",
                (name, updated_at, project_id, environment_id, instance_id))
            cursor = await db.execute("""UPDATE desktop_project_metadata SET display_name=?, updated_at=?
                WHERE project_id=? AND environment_id=? AND service_instance_id=?""",
                (name, updated_at, project_id, environment_id, instance_id))
            await db.commit()
            return cursor.rowcount == 1

    async def add_directory(self, project_id: str, record: dict):
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("""INSERT OR IGNORE INTO project_directories
                VALUES (?, ?, ?, ?, ?, ?)""", (project_id, record["root_path"], record["workspace_id"],
                record["device"], record["inode"], record["created_at"]))
            await db.commit()

    async def remove_directory(self, project_id: str, root_path: str):
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute("DELETE FROM project_directories WHERE project_id=? AND root_path=?", (project_id, root_path))
            await db.commit()
            return cursor.rowcount == 1

    async def list_directories(self, project_id: str):
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute("""SELECT root_path, workspace_id, device, inode, created_at
                FROM project_directories WHERE project_id=? ORDER BY root_path""", (project_id,))
            rows = await cursor.fetchall()
        keys = ("root_path", "workspace_id", "device", "inode", "created_at")
        return [dict(zip(keys, row)) for row in rows]

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
