from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


class WorkspaceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str
    repo_path: Path
    metadata_path: Path
    mode: Literal["managed", "original"] = "managed"
    pytest_paths: tuple[str, ...] = ()
    device: int | None = None
    inode: int | None = None


class WorkspaceFileListing(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    files: list[str]
    truncated: bool = False