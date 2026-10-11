"""Application-owned workspace metadata, compatible with managed records."""

import json
from pathlib import Path
from uuid import UUID


class WorkspaceRepository:
    def __init__(self, root: Path):
        self.root = root

    def directory(self, workspace_id: str) -> Path:
        try:
            if str(UUID(workspace_id)) != workspace_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("Invalid workspace id") from exc
        directory = self.root / workspace_id
        if directory.is_symlink() or not directory.resolve().is_relative_to(self.root):
            raise ValueError("Workspace boundary violation")
        if not directory.is_dir():
            raise KeyError("Workspace not found")
        return directory

    def load(self, workspace_id: str) -> tuple[Path, dict]:
        path = self.directory(workspace_id) / "metadata.json"
        if path.is_symlink() or not path.is_file():
            raise ValueError("Invalid workspace metadata")
        return path, json.loads(path.read_text(encoding="utf-8"))

    def find_original(self, repo_path: Path) -> str | None:
        if not self.root.exists():
            return None
        for directory in sorted(self.root.iterdir()):
            try:
                _, metadata = self.load(directory.name)
            except (KeyError, ValueError):
                continue
            if (metadata.get("mode") == "original" and metadata.get("status") == "active"
                    and metadata.get("repo_path") == str(repo_path)):
                return directory.name
        return None

    def create(self, workspace_id: str, metadata: dict) -> Path:
        directory = self.root / workspace_id
        directory.mkdir(parents=True)
        path = directory / "metadata.json"
        path.write_text(json.dumps(metadata), encoding="utf-8")
        return path