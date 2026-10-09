from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_id: UUID
    run_id: UUID
    workspace_id: str | None = None
    relative_path: str | None = None
    kind: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)
    created_at: datetime