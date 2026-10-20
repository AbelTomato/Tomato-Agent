import stat
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.projects.repository import ProjectRepository
from app.workspaces.service import WorkspacePathError, WorkspaceService


class ProjectPathError(ValueError):
    pass


class ProjectService:
    MAX_DIRECTORY_ENTRIES = 500

    def __init__(self, database_path: Path, allowed_roots, environment_id: str = "local",
                 service_instance_id: str = "local", workspace_service: WorkspaceService | None = None):
        if not environment_id or not service_instance_id:
            raise ValueError("Project identity is required")
        self.repository = ProjectRepository(Path(database_path))
        self.allowed_roots = tuple(Path(root).absolute() for root in allowed_roots)
        if any(not root.is_absolute() or root.resolve() == Path("/") for root in self.allowed_roots):
            raise ValueError("Project roots must be absolute and must not be /")
        self.environment_id = environment_id
        self.service_instance_id = service_instance_id
        self.workspace_service = workspace_service or WorkspaceService(
            Path(database_path).parent / "desktop-workspaces", allowed_roots=self.allowed_roots)

    async def initialize(self):
        await self.repository.init()

    async def _ensure_initialized(self):
        await self.repository.init()

    def _trusted_directory(self, path: Path) -> Path:
        path = Path(path)
        if not path.is_absolute():
            raise ProjectPathError("Project path must be absolute")
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise ProjectPathError("Symbolic links are not allowed")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ProjectPathError("Project directory is unavailable") from exc
        if not resolved.is_dir() or not any(resolved.is_relative_to(root.resolve()) for root in self.allowed_roots):
            raise ProjectPathError("Project path is outside approved roots")
        if any(root.is_symlink() for allowed in self.allowed_roots for root in (allowed, *allowed.parents)):
            raise ProjectPathError("Approved root contains a symbolic link")
        return resolved

    @staticmethod
    def _identity(path: Path):
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise ProjectPathError("Project path is not a directory")
        return info.st_dev, info.st_ino

    async def browse(self, path: Path, *, include_hidden: bool = False):
        await self._ensure_initialized()
        directory = self._trusted_directory(path)
        entries = []
        try:
            for index, child in enumerate(sorted(directory.iterdir(), key=lambda item: item.name.casefold())):
                if index >= self.MAX_DIRECTORY_ENTRIES:
                    break
                if not include_hidden and child.name.startswith("."):
                    continue
                if child.is_symlink() or not child.is_dir():
                    continue
                entries.append({"name": child.name, "path": str(child), "available": True})
        except PermissionError:
            raise
        return {"path": str(directory), "directories": entries}

    async def roots(self):
        await self._ensure_initialized()
        return [{"name": root.name or str(root), "path": str(root), "available": True}
                for root in self.allowed_roots if root.is_dir() and not root.is_symlink()]

    async def open(self, path: Path):
        await self._ensure_initialized()
        root = self._trusted_directory(path)
        device, inode = self._identity(root)
        existing = await self.repository.get_for_path(self.environment_id, self.service_instance_id, str(root))
        if existing:
            if (existing["device"], existing["inode"]) != (device, inode):
                raise ProjectPathError("Project directory identity changed")
            return existing
        return await self._register(root, device, inode)

    async def create_project(self, display_name: str):
        await self._ensure_initialized()
        name = display_name.strip()
        if not name or len(name) > 255 or "\x00" in name:
            raise ValueError("Project name must contain 1 to 255 characters")
        timestamp = datetime.now(timezone.utc).isoformat()
        record = {"project_id": str(uuid4()), "environment_id": self.environment_id,
            "service_instance_id": self.service_instance_id, "display_name": name,
            "created_at": timestamp, "updated_at": timestamp}
        return await self.repository.create_project(record)

    async def list_projects(self):
        await self._ensure_initialized()
        return await self.repository.list_projects(self.environment_id, self.service_instance_id)

    async def rename_project(self, project_id: str, display_name: str):
        await self._ensure_initialized()
        name = display_name.strip()
        if not name or len(name) > 255 or "\x00" in name:
            raise ValueError("Project name must contain 1 to 255 characters")
        updated = await self.repository.rename_project(project_id, self.environment_id,
            self.service_instance_id, name, datetime.now(timezone.utc).isoformat())
        if not updated:
            raise KeyError(project_id)
        return await self.repository.get_project(project_id, self.environment_id, self.service_instance_id)

    async def add_directory(self, project_id: str, path: Path):
        await self._ensure_initialized()
        project = await self.repository.get_project(project_id, self.environment_id, self.service_instance_id)
        if project is None:
            raise KeyError(project_id)
        root = self._trusted_directory(path)
        device, inode = self._identity(root)
        existing = await self.repository.list_directories(project_id)
        for directory in existing:
            if directory["root_path"] == str(root):
                if (directory["device"], directory["inode"]) != (device, inode):
                    raise ProjectPathError("Project directory identity changed")
                return directory
        workspace_id = self.workspace_service.repository.find_original(root)
        if workspace_id is None:
            workspace_id = (await self.workspace_service.register_original(root, [])).workspace_id
        record = {"root_path": str(root), "workspace_id": workspace_id, "device": device,
            "inode": inode, "created_at": datetime.now(timezone.utc).isoformat()}
        await self.repository.add_directory(project_id, record)
        return record

    async def remove_directory(self, project_id: str, path: Path):
        await self._ensure_initialized()
        project = await self.repository.get_project(project_id, self.environment_id, self.service_instance_id)
        if project is None:
            raise KeyError(project_id)
        root = Path(path).resolve(strict=False)
        return await self.repository.remove_directory(project_id, str(root))

    async def list_directories(self, project_id: str):
        await self._ensure_initialized()
        project = await self.repository.get_project(project_id, self.environment_id, self.service_instance_id)
        if project is None:
            raise KeyError(project_id)
        directories = await self.repository.list_directories(project_id)
        for directory in directories:
            try:
                root = self._trusted_directory(Path(directory["root_path"]))
                directory["available"] = self._identity(root) == (directory["device"], directory["inode"])
            except (OSError, ValueError, WorkspacePathError):
                directory["available"] = False
        return directories

    async def create(self, parent_path: Path, name: str):
        await self._ensure_initialized()
        parent = self._trusted_directory(parent_path)
        if (not name or name in {".", ".."} or Path(name).is_absolute() or "/" in name
                or "\\" in name or "\x00" in name):
            raise ValueError("Project name must be one directory name")
        root = parent / name
        root.mkdir()
        try:
            return await self.open(root)
        except Exception:
            # Filesystem creation is not rolled back if metadata persistence fails.
            raise

    async def list(self):
        await self._ensure_initialized()
        records = await self.repository.list_for_identity(self.environment_id, self.service_instance_id)
        available = []
        for record in records:
            try:
                root = self._trusted_directory(Path(record["root_path"]))
                identity = self._identity(root)
                record["available"] = identity == (record["device"], record["inode"])
            except (OSError, ValueError, WorkspacePathError):
                record["available"] = False
            available.append(record)
        return available

    async def _register(self, root: Path, device: int, inode: int):
        existing_workspace = self.workspace_service.repository.find_original(root)
        created_workspace = existing_workspace is None
        workspace = (await self.workspace_service.register_original(root, []) if created_workspace else
                     await self.workspace_service.get(existing_workspace))
        workspace_id = workspace.workspace_id
        timestamp = datetime.now(timezone.utc).isoformat()
        record = {"project_id": str(uuid4()), "workspace_id": workspace_id,
            "display_name": root.name, "root_path": str(root),
            "environment_id": self.environment_id, "service_instance_id": self.service_instance_id,
            "created_at": timestamp, "updated_at": timestamp, "device": device, "inode": inode}
        try:
            return await self.repository.create(record)
        except Exception:
            if created_workspace:
                await self.workspace_service.unregister(workspace_id)
            else:
                await self.repository.delete_for_path(self.environment_id, self.service_instance_id, str(root))
            raise
