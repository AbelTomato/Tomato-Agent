"""Persistent per-Run observations; file operations are not multi-file transactions.

Digest checks cannot exclude another writer between the final check and replace.
Uncommitted postimages remain pending until explicitly reconciled after executor exit.
"""

import difflib
import hashlib
import json
import os
import stat
import tempfile

import aiosqlite

from app.runs.repository import RunRepository
from .service import WorkspaceService


class FileChangeConflict(ValueError):
    pass


def digest(content: str | None) -> str:
    return "missing" if content is None else hashlib.sha256(content.encode("utf-8")).hexdigest()


class RunFileChanges:
    def __init__(self, workspaces: WorkspaceService, runs: RunRepository):
        self.workspaces = workspaces
        self.runs = runs
        self.path = workspaces.root / "run_changes.db"

    async def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self.path)
        try:
            await db.execute("""CREATE TABLE IF NOT EXISTS run_file_changes (
                run_id TEXT NOT NULL, workspace_id TEXT NOT NULL, path TEXT NOT NULL,
                initial TEXT, observed TEXT, agent TEXT, written INTEGER NOT NULL DEFAULT 0,
                pending TEXT, has_pending INTEGER NOT NULL DEFAULT 0,
                conflict INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(run_id, path))""")
            await db.commit()
        except BaseException:
            await db.close()
            raise
        return db

    async def _bind(self, run_id, workspace_id):
        run = await self.runs.get_run(run_id)
        if run is None or run.workspace_id != workspace_id:
            raise KeyError("Run workspace mismatch")
        self.workspaces.root_path(workspace_id)

    def _current(self, workspace_id, path):
        resolved = self.workspaces.resolve(workspace_id, path, write=True)
        if not resolved.exists():
            return None
        return self.workspaces._read_bytes(
            workspace_id, path, self.workspaces.max_file_bytes).decode("utf-8")

    async def _rows(self, run_id):
        db = await self._connect()
        try:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM run_file_changes WHERE run_id=? ORDER BY path", (str(run_id),))
            return [dict(row) for row in await cursor.fetchall()]
        finally:
            await db.close()

    async def _update(self, run_id, path, clause, values):
        db = await self._connect()
        try:
            await db.execute(f"UPDATE run_file_changes SET {clause} WHERE run_id=? AND path=?",
                             (*values, str(run_id), path))
            await db.commit()
        finally:
            await db.close()

    async def observe(self, run_id, workspace_id, path):
        await self._bind(run_id, workspace_id)
        # Canonical relative names prevent aliases from creating independent baselines.
        path = self.workspaces.resolve(workspace_id, path, write=True).relative_to(
            self.workspaces.root_path(workspace_id)).as_posix()
        row = next((row for row in await self._rows(run_id) if row["path"] == path), None)
        if row and row["has_pending"]:
            raise FileChangeConflict("write_pending_review")
        content = self._current(workspace_id, path)
        db = await self._connect()
        try:
            await db.execute("""INSERT INTO run_file_changes(run_id, workspace_id, path, initial, observed)
                VALUES(?,?,?,?,?) ON CONFLICT(run_id,path) DO UPDATE SET observed=excluded.observed""",
                (str(run_id), workspace_id, path, content, content))
            await db.commit()
        finally:
            await db.close()
        return content

    async def prepare_write(self, run_id, workspace_id, path, content):
        await self._bind(run_id, workspace_id)
        resolved = self.workspaces.resolve(workspace_id, path, write=True)
        path = resolved.relative_to(self.workspaces.root_path(workspace_id)).as_posix()
        if len(content.encode("utf-8")) > self.workspaces.max_file_bytes:
            raise ValueError("File too large")
        if "\x00" in content:
            raise ValueError("Binary content is not editable")
        row = next((row for row in await self._rows(run_id) if row["path"] == path), None)
        if row is None:
            raise FileChangeConflict("observation_required")
        if row["has_pending"]:
            raise FileChangeConflict("write_pending_review")
        current = self._current(workspace_id, path)
        if digest(current) != digest(row["observed"]):
            await self._update(run_id, path, "conflict=1", ())
            raise FileChangeConflict("file_changed")
        return path, current, content

    def _replace(self, workspace_id, path, before, after):
        target = self.workspaces.resolve(workspace_id, path, write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
        fd, temporary = tempfile.mkstemp(prefix=".agent-write-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(after.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), mode)
            # Re-resolve to reject replaced directories/links immediately before writing.
            checked = self.workspaces.resolve(workspace_id, path, write=True)
            if checked != target or digest(self._current(workspace_id, path)) != digest(before):
                raise FileChangeConflict("file_changed")
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    async def write_checked(self, run_id, workspace_id, path, content):
        path, before, after = await self.prepare_write(run_id, workspace_id, path, content)
        await self._update(run_id, path, "pending=?, has_pending=1", (after,))
        try:
            self._replace(workspace_id, path, before, after)
        except Exception:
            # Keep the intent: a filesystem error does not prove absence of a write.
            await self._update(run_id, path, "conflict=1", ())
            raise
        await self._update(run_id, path,
                           "agent=?, observed=?, written=1, pending=NULL, has_pending=0, conflict=0",
                           (after, after))

    async def prepare_patch(self, run_id, workspace_id, changes):
        pending = {}
        rows = {row["path"]: row for row in await self._rows(run_id)}
        for change in changes:
            path = self.workspaces.resolve(workspace_id, change["path"], write=True).relative_to(
                self.workspaces.root_path(workspace_id)).as_posix()
            if path in pending:
                raise ValueError("Duplicate patch path")
            row = rows.get(path)
            if row is None:
                raise FileChangeConflict("observation_required")
            content = row["observed"]
            if content is None or not change["old_text"] or content.count(change["old_text"]) != 1:
                raise ValueError("Patch must match exactly once")
            updated = content.replace(change["old_text"], change["new_text"], 1)
            await self.prepare_write(run_id, workspace_id, path, updated)
            pending[path] = updated
        return pending

    async def patch_checked(self, run_id, workspace_id, changes):
        pending = await self.prepare_patch(run_id, workspace_id, changes)
        for path, content in pending.items():
            await self.write_checked(run_id, workspace_id, path, content)
        return list(pending)

    async def reconcile_pending(self, run_id, workspace_id, path, *, executor_exited=False):
        await self._bind(run_id, workspace_id)
        if not executor_exited:
            raise FileChangeConflict("executor_exit_required")
        path = self.workspaces.resolve(workspace_id, path, write=True).relative_to(
            self.workspaces.root_path(workspace_id)).as_posix()
        row = next((row for row in await self._rows(run_id) if row["path"] == path), None)
        if row is None or not row["has_pending"]:
            raise FileChangeConflict("no_pending_write")
        actual = digest(self._current(workspace_id, path))
        if actual == digest(row["pending"]):
            await self._update(run_id, path,
                "agent=?, observed=?, written=1, pending=NULL, has_pending=0, conflict=0",
                (row["pending"], row["pending"]))
            return "postimage"
        if actual == digest(row["observed"]):
            await self._update(run_id, path, "pending=NULL, has_pending=0, conflict=0", ())
            return "preimage"
        await self._update(run_id, path, "conflict=1", ())
        raise FileChangeConflict("file_changed")

    async def conflicts_for_run(self, run_id):
        conflicts = []
        for row in await self._rows(run_id):
            await self._bind(run_id, row["workspace_id"])
            expected = row["agent"] if row["written"] else row["observed"]
            changed = digest(self._current(row["workspace_id"], row["path"])) != digest(expected)
            if changed or row["has_pending"]:
                conflicts.append(row["path"])
                await self._update(run_id, row["path"], "conflict=1", ())
        return conflicts

    async def diff_for_run(self, run_id):
        output = []
        for row in await self._rows(run_id):
            await self._bind(run_id, row["workspace_id"])
            before, after = row["initial"], row["agent"]
            if not row["written"] or before == after:
                continue
            name = row["path"]
            output.append(f"diff --git a/{name} b/{name}\n")
            if before is None:
                output.append("new file mode 100644\n")
            for line in difflib.unified_diff((before or "").splitlines(keepends=True),
                    after.splitlines(keepends=True), fromfile=f"a/{name}" if before is not None else "/dev/null",
                    tofile=f"b/{name}"):
                output.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
        await self.conflicts_for_run(run_id)
        return "".join(output)

    async def verification_manifest(self, run_id, workspace_id):
        await self._bind(run_id, workspace_id)
        files = {}
        for path in await self.workspaces.list_files(workspace_id):
            data = self.workspaces._read_bytes(workspace_id, path, self.workspaces.max_file_bytes)
            files[path] = hashlib.sha256(data).hexdigest()
        encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return {"files": files, "sha256": hashlib.sha256(encoded).hexdigest(),
                "filter_policy": "workspace_accessible_v1", "truncated": False}