import base64
import difflib
import json
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from .models import WorkspaceRecord


class WorkspacePathError(ValueError):
    """The requested path is outside the workspace file boundary."""


class WorkspaceService:
    def __init__(self, root: Path, max_file_bytes: int = 100_000):
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        self.root = Path(root).resolve()
        self.max_file_bytes = max_file_bytes

    def _directory(self, workspace_id: str) -> Path:
        try:
            if str(UUID(workspace_id)) != workspace_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise WorkspacePathError("Invalid workspace id") from exc
        directory = self.root / workspace_id
        if directory.is_symlink() or not directory.resolve().is_relative_to(self.root):
            raise WorkspacePathError("Workspace boundary violation")
        if not directory.is_dir():
            raise KeyError("Workspace not found")
        return directory

    def _metadata(self, workspace_id: str) -> tuple[Path, dict]:
        path = self._directory(workspace_id) / "metadata.json"
        if path.is_symlink() or not path.is_file():
            raise WorkspacePathError("Invalid workspace metadata")
        return path, json.loads(path.read_text(encoding="utf-8"))

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
        metadata = {"workspace_id": workspace_id, "status": "active", "baseline": {},
                    "artifact_refs": [], "events": []}
        record.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        # Links are preserved without following them; all operations reject them.
        for path in sorted(repo.rglob("*")):
            if path.is_symlink():
                continue
            if path.is_file():
                relative = path.relative_to(repo).as_posix()
                data = self._read_bytes(workspace_id, relative, self.max_file_bytes)
                metadata["baseline"][relative] = base64.b64encode(data).decode("ascii")
        record.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        return record

    def resolve(self, workspace_id: str, relative_path: str, write: bool = False) -> Path:
        _, metadata = self._metadata(workspace_id)
        if metadata["status"] != "active":
            raise KeyError("Workspace is not active")
        repo = self._directory(workspace_id) / "repo"
        path = Path(relative_path)
        if "\x00" in relative_path or path.is_absolute() or ".." in path.parts:
            raise WorkspacePathError("Only workspace-relative paths are allowed")
        if write and not path.parts:
            raise WorkspacePathError("A file path is required")
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
        directory = self.resolve(workspace_id, relative_dir)
        if not directory.is_dir():
            raise ValueError("Path must be a directory")
        repo = self.resolve(workspace_id, "")
        files = []
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(repo).as_posix()
            checked = self.resolve(workspace_id, relative)
            if checked.is_file():
                files.append(relative)
        return files

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