import base64
import difflib
import json
import os
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .models import WorkspaceFileListing, WorkspaceRecord
from .repository import WorkspaceRepository


class WorkspacePathError(ValueError):
    """The requested path is outside the workspace file boundary."""


class WorkspaceService:
    def __init__(self, root: Path, max_file_bytes: int = 100_000, *,
                 allowed_roots: list[Path] | tuple[Path, ...] = (), max_files: int = 10_000):
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        self.root = Path(root).resolve()
        self.max_file_bytes = max_file_bytes
        if max_files <= 0:
            raise ValueError("max_files must be positive")
        self.max_files = max_files
        self.allowed_roots = tuple(Path(path).absolute() for path in allowed_roots)
        if any(not Path(path).is_absolute() or Path(path).resolve() == Path("/")
               for path in allowed_roots):
            raise ValueError("Allowed roots must be absolute project directories, not /")
        self.repository = WorkspaceRepository(self.root)

    def _directory(self, workspace_id: str) -> Path:
        try:
            return self.repository.directory(workspace_id)
        except ValueError as exc:
            raise WorkspacePathError(str(exc)) from exc

    def _metadata(self, workspace_id: str) -> tuple[Path, dict]:
        try:
            return self.repository.load(workspace_id)
        except ValueError as exc:
            raise WorkspacePathError(str(exc)) from exc

    @staticmethod
    def _reject_links(path: Path) -> None:
        if any(component.is_symlink() for component in (path, *path.parents)):
            raise WorkspacePathError("Symbolic links are not allowed")

    @staticmethod
    def _blocked(path: Path) -> bool:
        templates = {".env.example", ".env.sample", ".env.template"}
        blocked = {".git", ".ssh", ".aws", ".azure", ".gcloud", "node_modules", ".venv",
                   "__pycache__", "credentials", "credentials.json", "credentials.yaml",
                   "credentials.yml", ".netrc", ".npmrc", ".pypirc", "secrets.json",
                   "service-account.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
        return any(part.lower() in blocked or
                   (part.lower().startswith(".env") and part.lower() not in templates) or
                   Path(part).suffix.lower() in {".pem", ".key", ".p12", ".pfx"}
                   for part in path.parts)

    async def register_original(self, path: Path, pytest_paths: list[str]) -> WorkspaceRecord:
        source = Path(path)
        if not source.is_absolute():
            raise ValueError("Original path must be absolute")
        self._reject_links(source)
        source = source.resolve()
        if not source.is_dir():
            raise ValueError("Original path must be a directory")
        if not any(source.is_relative_to(root.resolve()) for root in self.allowed_roots):
            raise WorkspacePathError("Project is outside workspace_allowed_roots")
        for root in self.allowed_roots:
            self._reject_links(root)
        if self.root.is_relative_to(source) or source.is_relative_to(self.root):
            raise WorkspacePathError("Workspace metadata and original project must be separate")
        if not pytest_paths:
            raise ValueError("pytest_paths must not be empty")
        targets = []
        for value in pytest_paths:
            target = Path(value)
            if (not value or "\x00" in value or value.startswith("-") or target.is_absolute()
                    or ".." in target.parts):
                raise ValueError("Invalid pytest path")
            if self._blocked(target):
                raise WorkspacePathError("Restricted pytest path")
            candidate = source / target
            self._reject_links(candidate)
            if not candidate.exists() or not (candidate.is_dir() or candidate.is_file()):
                raise ValueError("pytest path must exist")
            if candidate.is_file() and candidate.stat().st_nlink != 1:
                raise WorkspacePathError("Hard links are not allowed")
            targets.append(target.as_posix())
        existing = self.repository.find_original(source)
        if existing:
            record = await self.get(existing)
            if record.pytest_paths != tuple(targets):
                raise ValueError("Workspace already registered with different pytest_paths")
            return record
        identity = source.stat()
        workspace_id = str(uuid4())
        self.repository.create(workspace_id, {
            "workspace_id": workspace_id, "mode": "original", "status": "active",
            "repo_path": str(source), "device": identity.st_dev, "inode": identity.st_ino,
            "pytest_paths": targets, "artifact_refs": [], "events": [],
        })
        return await self.get(workspace_id)

    def root_path(self, workspace_id: str) -> Path:
        _, metadata = self._metadata(workspace_id)
        if metadata["status"] != "active":
            raise KeyError("Workspace is not active")
        if metadata.get("mode", "managed") == "original":
            repo = Path(metadata["repo_path"])
            try:
                self._reject_links(repo)
                identity = repo.stat()
                if not repo.is_dir() or (identity.st_dev, identity.st_ino) != (
                        metadata["device"], metadata["inode"]):
                    raise WorkspacePathError("workspace_changed")
            except (OSError, WorkspacePathError) as exc:
                raise WorkspacePathError("workspace_changed") from exc
            return repo
        repo = self._directory(workspace_id) / "repo"
        self._reject_links(repo)
        if not repo.is_dir():
            raise WorkspacePathError("Invalid workspace repo")
        return repo

    async def get(self, workspace_id: str) -> WorkspaceRecord:
        metadata_path, metadata = self._metadata(workspace_id)
        return WorkspaceRecord(workspace_id=workspace_id, repo_path=self.root_path(workspace_id),
            metadata_path=metadata_path, mode=metadata.get("mode", "managed"),
            pytest_paths=tuple(metadata.get("pytest_paths", [])),
            device=metadata.get("device"), inode=metadata.get("inode"))

    async def unregister(self, workspace_id: str) -> None:
        path, metadata = self._metadata(workspace_id)
        if metadata.get("mode", "managed") != "original":
            raise ValueError("Only original workspaces can be unregistered")
        metadata["status"] = "unregistered"
        path.write_text(json.dumps(metadata), encoding="utf-8")

    async def create(self, input_root: Path | None = None) -> WorkspaceRecord:
        source = Path(input_root).resolve() if input_root is not None else None
        if source is not None and not source.is_dir():
            raise ValueError("input_root must be a directory")
        if source is not None and self.root.is_relative_to(source):
            raise WorkspacePathError("Workspace storage cannot be inside input_root")
        workspace_id = str(uuid4())
        directory = self.root / workspace_id
        directory.mkdir(parents=True)
        repo = directory / "repo"
        if source is None:
            repo.mkdir()
        else:
            shutil.copytree(source, repo, symlinks=True)
        record = WorkspaceRecord(
            workspace_id=workspace_id, repo_path=repo, metadata_path=directory / "metadata.json"
        )
        metadata = {"workspace_id": workspace_id, "mode": "managed", "status": "active", "baseline": {},
                    "artifact_refs": [], "events": []}
        record.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        # Links are preserved without following them; all operations reject them.
        for relative in await self.list_files(workspace_id):
            data = self._read_bytes(workspace_id, relative, self.max_file_bytes)
            metadata["baseline"][relative] = base64.b64encode(data).decode("ascii")
        record.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        return record

    def resolve(self, workspace_id: str, relative_path: str, write: bool = False) -> Path:
        _, metadata = self._metadata(workspace_id)
        if metadata["status"] != "active":
            raise KeyError("Workspace is not active")
        repo = self.root_path(workspace_id)
        path = Path(relative_path)
        if "\x00" in relative_path or path.is_absolute() or ".." in path.parts:
            raise WorkspacePathError("Only workspace-relative paths are allowed")
        if write and not path.parts:
            raise WorkspacePathError("A file path is required")
        if self._blocked(path):
            raise WorkspacePathError("Restricted workspace path")
        candidate = repo / path
        if repo.is_symlink() or not repo.is_dir():
            raise WorkspacePathError("Invalid workspace repo")
        for component in (candidate, *candidate.parents):
            if component == repo.parent:
                break
            if component.is_symlink():
                raise WorkspacePathError("Symbolic links are not allowed")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(repo.resolve()):
            raise WorkspacePathError("Path escapes workspace")
        if resolved.exists() and resolved.is_file() and resolved.stat().st_nlink != 1:
            raise WorkspacePathError("Hard links are not allowed")
        return resolved

    async def list_files(self, workspace_id: str, relative_dir: str = "") -> list[str]:
        listing = await self.enumerate_files(workspace_id, relative_dir)
        if listing.truncated:
            raise ValueError("enumeration_truncated")
        return listing.files

    async def enumerate_files(self, workspace_id: str, relative_dir: str = "") -> WorkspaceFileListing:
        directory = self.resolve(workspace_id, relative_dir)
        if not directory.is_dir():
            raise ValueError("Path must be a directory")
        repo = self.resolve(workspace_id, "")
        files = []
        def traversal_error(error: OSError) -> None:
            raise error

        for current, directories, names in os.walk(directory, followlinks=False, onerror=traversal_error):
            directories[:] = sorted(name for name in directories
                if not self._blocked(Path(current, name).relative_to(repo))
                and not Path(current, name).is_symlink())
            for name in sorted(names):
                path = Path(current, name)
                relative = path.relative_to(repo)
                if self._blocked(relative) or path.is_symlink():
                    continue
                checked = self.resolve(workspace_id, relative.as_posix())
                if checked.is_file():
                    if len(files) == self.max_files:
                        return WorkspaceFileListing(files=sorted(files), truncated=True)
                    files.append(relative.as_posix())
        return WorkspaceFileListing(files=sorted(files))

    def _read_bytes(self, workspace_id: str, relative_path: str, max_bytes: int) -> bytes:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        path = self.resolve(workspace_id, relative_path)
        if not stat.S_ISREG(path.stat().st_mode):
            raise WorkspacePathError("Only regular files are allowed")
        limit = min(max_bytes, self.max_file_bytes)
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ValueError("File too large")
        return data

    async def read_file(self, workspace_id: str, relative_path: str, max_bytes: int) -> str:
        return self._read_bytes(workspace_id, relative_path, max_bytes).decode("utf-8")

    async def write_file(self, workspace_id: str, relative_path: str, content: str) -> None:
        data = content.encode("utf-8")
        if len(data) > self.max_file_bytes:
            raise ValueError("File too large")
        path = self.resolve(workspace_id, relative_path, write=True)
        if path.exists() and not path.is_file():
            raise WorkspacePathError("Only regular files are allowed")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def diff(self, workspace_id: str) -> str:
        _, metadata = self._metadata(workspace_id)
        if metadata.get("mode") == "original":
            raise ValueError("Original workspace diff requires a Run baseline")
        current = {name: self._read_bytes(workspace_id, name, self.max_file_bytes)
                   for name in await self.list_files(workspace_id)}
        baseline = {name: base64.b64decode(data) for name, data in metadata["baseline"].items()}
        output = []
        for name in sorted(baseline.keys() | current.keys()):
            old, new = baseline.get(name, b""), current.get(name, b"")
            if old == new and (name in baseline) == (name in current):
                continue
            try:
                if b"\x00" in old or b"\x00" in new:
                    raise ValueError("Cannot generate binary diff")
                before, after = old.decode("utf-8"), new.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Cannot generate binary diff") from exc
            output.append(f"diff --git a/{name} b/{name}\n")
            if name not in baseline:
                output.append("new file mode 100644\n")
            elif name not in current:
                output.append("deleted file mode 100644\n")
            lines = difflib.unified_diff(
                before.splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile=f"a/{name}" if name in baseline else "/dev/null",
                tofile=f"b/{name}" if name in current else "/dev/null",
            )
            for line in lines:
                output.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
        return "".join(output)

    async def cleanup(self, workspace_id: str) -> None:
        path, metadata = self._metadata(workspace_id)
        if metadata.get("mode") == "original":
            raise ValueError("Cannot cleanup original workspace; use unregister")
        if metadata["status"] == "cleaned":
            return
        repo = self.resolve(workspace_id, "")
        try:
            shutil.rmtree(repo)
        except OSError:
            metadata["events"].append({"event_type": "workspace.cleanup_failed",
                                       "created_at": datetime.now(timezone.utc).isoformat(),
                                       "error_code": "cleanup_failed"})
            path.write_text(json.dumps(metadata), encoding="utf-8")
            raise
        metadata["status"] = "cleaned"
        metadata["events"].append({"event_type": "workspace.cleaned",
                                   "created_at": datetime.now(timezone.utc).isoformat()})
        path.write_text(json.dumps(metadata), encoding="utf-8")

    async def record_artifact_ref(self, workspace_id: str, artifact_ref: dict) -> None:
        path, metadata = self._metadata(workspace_id)
        if metadata["status"] != "active":
            raise KeyError("Workspace is not active")
        artifact_id = artifact_ref.get("artifact_id")
        if not artifact_id:
            raise ValueError("artifact_ref requires artifact_id")
        if all(item.get("artifact_id") != artifact_id for item in metadata["artifact_refs"]):
            metadata["artifact_refs"].append(artifact_ref)
            path.write_text(json.dumps(metadata), encoding="utf-8")