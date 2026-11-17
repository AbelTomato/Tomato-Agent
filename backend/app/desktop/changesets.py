import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MAX_CHANGE_BYTES = 256 * 1024
MAX_CHANGESET_BYTES = 512 * 1024
_SHA256 = r"^[0-9a-f]{64}$"


class ProposedFileChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    relative_path: str = Field(min_length=1, max_length=4096)
    operation: Literal["add", "modify"]
    expected_old_sha256: str | None
    base_content: str | None = Field(max_length=MAX_CHANGE_BYTES)
    content: str = Field(max_length=MAX_CHANGE_BYTES)
    summary: str = Field(default="", max_length=1000)

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (value.startswith("/") or "\\" in value or ":" in value or
                "\x00" in value or ".." in path.parts or
                any(part in {"", "."} for part in value.split("/")) or
                any(_sensitive_name(part) for part in path.parts)):
            raise ValueError("unsafe_changeset_path")
        return value

    @field_validator("expected_old_sha256")
    @classmethod
    def validate_old_hash(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(_SHA256, value):
            raise ValueError("invalid_expected_old_sha256")
        return value

    @model_validator(mode="after")
    def validate_operation_condition(self):
        if self.operation == "add" and self.expected_old_sha256 is not None:
            raise ValueError("add_requires_absent_target")
        if self.operation == "add" and self.base_content is not None:
            raise ValueError("add_requires_empty_base")
        if self.operation == "modify":
            if self.expected_old_sha256 is None or self.base_content is None:
                raise ValueError("modify_requires_old_content_and_hash")
            actual = hashlib.sha256(self.base_content.encode("utf-8")).hexdigest()
            if actual != self.expected_old_sha256:
                raise ValueError("base_content_hash_mismatch")
        if "\x00" in self.content or (self.base_content is not None and "\x00" in self.base_content):
            raise ValueError("invalid_change_content")
        return self


class ChangeSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    change_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    authorization_version: str = Field(min_length=1, max_length=200)
    changes: tuple[ProposedFileChange, ...] = Field(min_length=1, max_length=100, strict=False)

    @model_validator(mode="after")
    def validate_paths_and_budget(self):
        paths = [change.relative_path for change in self.changes]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate_changeset_path")
        total = sum(
            len(change.content.encode("utf-8")) +
            (len(change.base_content.encode("utf-8")) if change.base_content is not None else 0)
            for change in self.changes
        )
        if total > MAX_CHANGESET_BYTES:
            raise ValueError("changeset_too_large")
        return self

    def canonical_payload(self) -> dict:
        return self.model_dump(mode="json")

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            self.canonical_payload(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


def _sensitive_name(name: str) -> bool:
    lower = name.lower()
    return (lower in {".git", ".svn", ".hg", ".ssh", ".aws", ".azure", "secrets"}
            or lower == ".env" or lower.startswith(".env."))
