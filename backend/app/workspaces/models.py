from pathlib import Path

from pydantic import BaseModel, ConfigDict


class WorkspaceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str
    repo_path: Path
    metadata_path: Path