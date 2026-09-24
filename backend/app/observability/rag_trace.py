from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Literal, TypeAlias
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

TRACE_SCHEMA = "rag-observe-trace/v1"
TraceSchema: TypeAlias = Literal["rag-observe-trace/v1"]
RunStatus: TypeAlias = Literal["running", "complete", "incomplete"]
EventStatus: TypeAlias = Literal["success", "skipped", "failed"]
ReviewStatus: TypeAlias = Literal["pass", "partial", "fail", "not_reviewed"]
LLMPurpose: TypeAlias = Literal["query_planner", "answerer"]
LLMCallStatus: TypeAlias = Literal["success", "failed", "skipped"]
# Pydantic v2 cannot build a schema for a recursive TypeAlias on Python 3.11.
# Every field using this alias is guarded by _validate_json_value before parsing.
JSONValue: TypeAlias = Any

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_FIELD_PARTS = {
    "authorization",
    "credential",
    "key",
    "password",
    "secret",
    "token",
}
_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_FIELD_PARTS = re.compile(r"[^A-Za-z0-9]+")


class _TraceModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def _validate_nonblank(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value


def _validate_sha256(value: str, field_name: str) -> str:
    if not _SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest")
    return value


def _validate_utc_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


def _has_sensitive_field_name(name: str) -> bool:
    separated = _CAMEL_BOUNDARY.sub(r"\1_\2", name)
    parts = {part.lower() for part in _FIELD_PARTS.split(separated) if part}
    return bool(parts & _SENSITIVE_FIELD_PARTS)


def _validate_json_value(value: object, path: str = "value", ancestors: set[int] | None = None) -> None:
    """Reject non-JSON Python objects, non-finite floats, cycles, and credential keys."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{path} must not contain NaN or Infinity")
        return

    if ancestors is None:
        ancestors = set()
    if isinstance(value, (dict, list)):
        object_id = id(value)
        if object_id in ancestors:
            raise ValueError(f"{path} must not contain circular references")
        ancestors.add(object_id)
        try:
            if isinstance(value, dict):
                for key, nested_value in value.items():
                    if not isinstance(key, str):
                        raise ValueError(f"{path} object keys must be strings")
                    if _has_sensitive_field_name(key):
                        raise ValueError(f"{path} contains a sensitive field name")
                    _validate_json_value(nested_value, f"{path}.{key}", ancestors)
            else:
                for index, nested_value in enumerate(value):
                    _validate_json_value(nested_value, f"{path}[{index}]", ancestors)
        finally:
            ancestors.remove(object_id)
        return

    raise ValueError(f"{path} contains a non-JSON value")


class _FrozenDict(dict):
    """A JSON object that remains JSON-encoder compatible but cannot be mutated."""

    def _immutable(self, *_args, **_kwargs):
        raise TypeError("trace JSON values are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


class _FrozenList(list):
    """A JSON array that remains JSON-encoder compatible but cannot be mutated."""

    def _immutable(self, *_args, **_kwargs):
        raise TypeError("trace JSON values are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable


def _freeze_json(value):
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze_json(nested) for key, nested in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze_json(nested) for nested in value)
    return value


def canonical_json_bytes(value: object) -> bytes:
    """Serialize JSON-compatible values with the trace canonical encoding."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    _validate_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_sha256(value: object) -> str:
    """Return the SHA-256 hex digest of canonical UTF-8 JSON bytes."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class DataFingerprint(_TraceModel):
    path_identifier: str = Field(min_length=1)
    sha256: str
    version: str = Field(min_length=1)
    manifest_sha256: str

    @field_validator("path_identifier", "version")
    @classmethod
    def validate_nonblank_strings(cls, value: str, info) -> str:
        return _validate_nonblank(value, info.field_name)

    @field_validator("sha256", "manifest_sha256")
    @classmethod
    def validate_hashes(cls, value: str, info) -> str:
        return _validate_sha256(value, info.field_name)


class ArtifactDigest(_TraceModel):
    relative_path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        _validate_nonblank(value, "relative_path")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value:
            raise ValueError("artifact path must be a safe relative POSIX path")
        if value in (".", ""):
            raise ValueError("artifact path must identify a file")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _validate_sha256(value, "sha256")


class PromptIdentity(_TraceModel):
    prompt_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    sha256: str

    @field_validator("prompt_id", "version")
    @classmethod
    def validate_nonblank_strings(cls, value: str, info) -> str:
        return _validate_nonblank(value, info.field_name)

    @field_validator("sha256")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _validate_sha256(value, "sha256")


class TraceEvent(_TraceModel):
    schema_version: TraceSchema
    run_id: UUID
    question_id: str = Field(min_length=1)
    sequence: int = Field(gt=0)
    occurred_at: datetime
    event_type: str = Field(min_length=1)
    status: EventStatus
    duration_ms: float | None = Field(default=None, ge=0.0)
    payload: dict[str, JSONValue]
    previous_event_sha256: str | None = None
    event_sha256: str

    @field_validator("question_id", "event_type")
    @classmethod
    def validate_nonblank_strings(cls, value: str, info) -> str:
        return _validate_nonblank(value, info.field_name)

    @field_validator("occurred_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _validate_utc_datetime(value, "occurred_at")

    @field_validator("duration_ms")
    @classmethod
    def validate_finite_duration(cls, value: float | None) -> float | None:
        if value is not None and (value != value or value in (float("inf"), float("-inf"))):
            raise ValueError("duration_ms must be finite")
        return value

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload_json(cls, value: object) -> object:
        if not isinstance(value, dict):
            raise ValueError("payload must be a JSON object")
        _validate_json_value(value, "payload")
        return value

    @field_validator("payload")
    @classmethod
    def freeze_payload(cls, value: dict[str, JSONValue]) -> dict[str, JSONValue]:
        return _freeze_json(value)

    @field_validator("previous_event_sha256")
    @classmethod
    def validate_previous_hash(cls, value: str | None) -> str | None:
        if value is not None:
            return _validate_sha256(value, "previous_event_sha256")
        return value

    @field_validator("event_sha256")
    @classmethod
    def validate_event_hash(cls, value: str) -> str:
        return _validate_sha256(value, "event_sha256")


class LLMCallRecord(_TraceModel):
    call_id: str = Field(min_length=1)
    run_id: UUID
    question_id: str = Field(min_length=1)
    purpose: LLMPurpose
    attempt: int = Field(ge=1)
    model_id: str = Field(min_length=1)
    prompt: PromptIdentity
    messages: list[JSONValue]
    tools: list[JSONValue]
    messages_sha256: str
    tools_sha256: str
    status: LLMCallStatus
    duration_ms: float | None = Field(default=None, ge=0.0)
    response: JSONValue | None = None
    error_code: str | None = None

    @field_validator("call_id", "question_id", "model_id")
    @classmethod
    def validate_nonblank_strings(cls, value: str, info) -> str:
        return _validate_nonblank(value, info.field_name)

    @field_validator("messages", "tools", "response", mode="before")
    @classmethod
    def validate_json_content(cls, value: object, info) -> object:
        if value is not None:
            _validate_json_value(value, info.field_name)
        return value

    @field_validator("messages", "tools", "response")
    @classmethod
    def freeze_json_content(cls, value):
        return _freeze_json(value)

    @field_validator("messages_sha256", "tools_sha256")
    @classmethod
    def validate_hashes(cls, value: str, info) -> str:
        return _validate_sha256(value, info.field_name)

    @field_validator("duration_ms")
    @classmethod
    def validate_finite_duration(cls, value: float | None) -> float | None:
        if value is not None and (value != value or value in (float("inf"), float("-inf"))):
            raise ValueError("duration_ms must be finite")
        return value

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_nonblank(value, "error_code")
            if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", value):
                raise ValueError("error_code must be a stable lowercase code")
        return value

    @model_validator(mode="after")
    def validate_hashes_and_status(self) -> LLMCallRecord:
        if canonical_json_sha256(self.messages) != self.messages_sha256:
            raise ValueError("messages_sha256 does not match messages")
        if canonical_json_sha256(self.tools) != self.tools_sha256:
            raise ValueError("tools_sha256 does not match tools")
        if self.status == "success" and self.error_code is not None:
            raise ValueError("successful LLM calls must not include error_code")
        if self.status == "failed" and self.error_code is None:
            raise ValueError("failed LLM calls require error_code")
        return self


class HumanAnswerReview(_TraceModel):
    question_id: str = Field(min_length=1)
    reviewer_id: str | None = None
    reviewed_at: datetime | None = None
    correctness: ReviewStatus
    completeness: ReviewStatus
    groundedness: ReviewStatus
    citation_fidelity: ReviewStatus
    citation_chunk_ids: tuple[str, ...] = ()
    rationale: str | None = Field(default=None, max_length=1000)

    @field_validator("question_id", "reviewer_id")
    @classmethod
    def validate_nonblank_strings(cls, value: str | None, info) -> str | None:
        if value is not None:
            _validate_nonblank(value, info.field_name)
        return value

    @field_validator("reviewed_at")
    @classmethod
    def validate_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            return _validate_utc_datetime(value, "reviewed_at")
        return value

    @field_validator("citation_chunk_ids")
    @classmethod
    def validate_citation_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not chunk_id.strip() for chunk_id in value):
            raise ValueError("citation_chunk_ids must not contain blank values")
        if len(value) != len(set(value)):
            raise ValueError("citation_chunk_ids must be unique")
        return value

    @field_validator("rationale")
    @classmethod
    def validate_rationale(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_nonblank(value, "rationale")
            _validate_json_value({"rationale": value}, "review")
        return value

    @model_validator(mode="after")
    def validate_review_state(self) -> HumanAnswerReview:
        statuses = (
            self.correctness,
            self.completeness,
            self.groundedness,
            self.citation_fidelity,
        )
        is_pending = all(status == "not_reviewed" for status in statuses)
        if is_pending != (self.reviewer_id is None and self.reviewed_at is None):
            raise ValueError("unreviewed answers require no reviewer/time; reviewed answers require both")
        if (self.reviewer_id is None) != (self.reviewed_at is None):
            raise ValueError("reviewer_id and reviewed_at must be provided together")
        return self


class TraceRunManifest(_TraceModel):
    trace_schema: TraceSchema
    run_id: UUID
    environment: Literal["dev"]
    split: Literal["dev"]
    started_at: datetime
    finished_at: datetime | None
    status: RunStatus
    dataset: DataFingerprint
    snapshot: DataFingerprint
    implementation: dict[str, JSONValue]
    configuration: dict[str, JSONValue]
    artifacts: tuple[ArtifactDigest, ...] = ()

    @field_validator("started_at", "finished_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None, info) -> datetime | None:
        if value is not None:
            return _validate_utc_datetime(value, info.field_name)
        return value

    @field_validator("implementation", "configuration", mode="before")
    @classmethod
    def validate_json_objects(cls, value: object, info) -> object:
        if not isinstance(value, dict):
            raise ValueError(f"{info.field_name} must be a JSON object")
        _validate_json_value(value, info.field_name)
        return value

    @field_validator("implementation", "configuration")
    @classmethod
    def freeze_json_objects(cls, value):
        return _freeze_json(value)

    @model_validator(mode="after")
    def validate_run_state(self) -> TraceRunManifest:
        if (self.status == "running") != (self.finished_at is None):
            raise ValueError("running manifests have no finished_at; terminal manifests require it")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        artifact_paths = [artifact.relative_path for artifact in self.artifacts]
        if "manifest.json" in artifact_paths:
            raise ValueError("manifest.json must not include its own artifact digest")
        if len(artifact_paths) != len(set(artifact_paths)):
            raise ValueError("artifact paths must be unique")
        return self
