import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import aiosqlite

from app.workspaces.service import WorkspacePathError, WorkspaceService

from .models import ArtifactRef


class ArtifactNotFoundError(KeyError):
    """Raised when an artifact id is unknown."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ArtifactService:
    def __init__(
        self,
        root: Path,
        workspace_service: WorkspaceService,
        max_bytes: int = 1_000_000,
        database_path: Path | None = None,
    ):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.root = Path(root).resolve()
        self.storage = self.root / "files"
        self.database_path = Path(database_path or self.root / "artifacts.db")
        self.workspace_service = workspace_service
        self.max_bytes = max_bytes

    async def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.storage.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    workspace_id TEXT,
                    relative_path TEXT,
                    kind TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    storage_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, workspace_id, relative_path, kind, sha256)
                )
                """
            )
            await db.commit()

    @staticmethod
    def _validate_run_id(run_id: UUID | str) -> UUID:
        try:
            return run_id if isinstance(run_id, UUID) else UUID(str(run_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("run_id must be a UUID") from exc

    def _check_content(self, content: bytes) -> None:
        if len(content) > self.max_bytes:
            raise ValueError("Artifact too large")

    @staticmethod
    def _digest(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    async def _register(
        self,
        run_id: UUID | str,
        kind: str,
        content: bytes,
        workspace_id: str | None = None,
        relative_path: str | None = None,
    ) -> ArtifactRef:
        if not kind:
            raise ValueError("kind must not be empty")
        run_uuid = self._validate_run_id(run_id)
        self._check_content(content)
        digest = self._digest(content)
        created_at = _now()
        artifact_id = uuid4()
        storage_path = self.storage / f"{artifact_id}.bin"

        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute(
                "SELECT artifact_id, run_id, workspace_id, relative_path, kind, size_bytes, sha256, created_at "
                "FROM artifacts WHERE run_id = ? AND workspace_id IS ? AND relative_path IS ? AND kind = ? AND sha256 = ?",
                (str(run_uuid), workspace_id, relative_path, kind, digest),
            )
            row = await cursor.fetchone()
            if row is not None:
                return self._row_to_ref(row)
            storage_path.write_bytes(content)
            try:
                await db.execute(
                    "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(artifact_id), str(run_uuid), workspace_id, relative_path,
                        kind, len(content), digest, str(storage_path), created_at,
                    ),
                )
                await db.commit()
            except sqlite3.IntegrityError:
                storage_path.unlink(missing_ok=True)
                cursor = await db.execute(
                    "SELECT artifact_id, run_id, workspace_id, relative_path, kind, size_bytes, sha256, created_at "
                    "FROM artifacts WHERE run_id = ? AND workspace_id IS ? AND relative_path IS ? AND kind = ? AND sha256 = ?",
                    (str(run_uuid), workspace_id, relative_path, kind, digest),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise
                return self._row_to_ref(row)

        ref = ArtifactRef(
            artifact_id=artifact_id, run_id=run_uuid, workspace_id=workspace_id,
            relative_path=relative_path, kind=kind, size_bytes=len(content),
            sha256=digest, created_at=datetime.fromisoformat(created_at),
        )
        if workspace_id is not None:
            await self.workspace_service.record_artifact_ref(workspace_id, ref.model_dump(mode="json"))
        return ref

    async def register_file(
        self, run_id: UUID | str, workspace_id: str, relative_path: str, kind: str
    ) -> ArtifactRef:
        if Path(relative_path).is_absolute():
            raise WorkspacePathError("Only workspace-relative paths are allowed")
        path = self.workspace_service.resolve(workspace_id, relative_path)
        if not path.is_file():
            raise WorkspacePathError("Only regular files are allowed")
        content = path.read_bytes()
        return await self._register(run_id, kind, content, workspace_id, Path(relative_path).as_posix())

    async def register_text(self, run_id: UUID | str, kind: str, content: str) -> ArtifactRef:
        return await self._register(run_id, kind, content.encode("utf-8"))

    @staticmethod
    def _row_to_ref(row: tuple) -> ArtifactRef:
        return ArtifactRef(
            artifact_id=UUID(row[0]), run_id=UUID(row[1]), workspace_id=row[2],
            relative_path=row[3], kind=row[4], size_bytes=row[5], sha256=row[6],
            created_at=datetime.fromisoformat(row[7]),
        )

    async def get(self, artifact_id: UUID | str) -> ArtifactRef:
        try:
            artifact_uuid = artifact_id if isinstance(artifact_id, UUID) else UUID(str(artifact_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ArtifactNotFoundError(str(artifact_id)) from exc
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute(
                "SELECT artifact_id, run_id, workspace_id, relative_path, kind, size_bytes, sha256, created_at "
                "FROM artifacts WHERE artifact_id = ?", (str(artifact_uuid),)
            )
            row = await cursor.fetchone()
        if row is None:
            raise ArtifactNotFoundError(str(artifact_id))
        return self._row_to_ref(row)

    async def read(self, artifact_id: UUID | str, max_bytes: int) -> bytes:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        ref = await self.get(artifact_id)
        if ref.size_bytes > min(max_bytes, self.max_bytes):
            raise ValueError("Artifact too large")
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute("SELECT storage_path FROM artifacts WHERE artifact_id = ?", (str(ref.artifact_id),))
            row = await cursor.fetchone()
        if row is None:
            raise ArtifactNotFoundError(str(artifact_id))
        path = Path(row[0]).resolve()
        if not path.is_relative_to(self.storage) or not path.is_file():
            raise ArtifactNotFoundError(str(artifact_id))
        content = path.read_bytes()
        if len(content) != ref.size_bytes or self._digest(content) != ref.sha256:
            raise ValueError("Artifact integrity check failed")
        return content

    async def list_for_run(self, run_id: UUID | str) -> list[ArtifactRef]:
        run_uuid = self._validate_run_id(run_id)
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute(
                "SELECT artifact_id, run_id, workspace_id, relative_path, kind, size_bytes, sha256, created_at "
                "FROM artifacts WHERE run_id = ? ORDER BY created_at, artifact_id", (str(run_uuid),)
            )
            rows = await cursor.fetchall()
        return [self._row_to_ref(row) for row in rows]