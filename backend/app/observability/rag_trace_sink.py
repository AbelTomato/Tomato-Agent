"""Private, offline-only persistence for versioned RAG trace runs.

This module deliberately has no online-service integration. A caller must pass
an explicit reports directory under ``backend/data/rag/reports``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import quote
from uuid import UUID

from pydantic import ValidationError

from app.observability.rag_trace import (
    ArtifactDigest,
    DataFingerprint,
    HumanAnswerReview,
    TRACE_SCHEMA,
    TraceEvent,
    TraceRunManifest,
    canonical_json_bytes,
    canonical_json_sha256,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REPORTS_ROOT = (PROJECT_ROOT / "backend" / "data" / "rag" / "reports").resolve()
_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DATASET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class TraceSinkError(RuntimeError):
    """Raised when a run cannot be safely created or written."""


class TraceIntegrityError(TraceSinkError):
    """Raised when persisted trace artifacts fail verification."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_question_filename(question_id: str) -> str:
    # Percent-encode separators and other path syntax while preserving stable IDs.
    encoded = quote(question_id, safe="-_.")
    if not encoded or encoded in {".", ".."}:
        raise TraceSinkError("invalid question id")
    return f"{encoded}.json"


def _validate_payload(payload: object, *, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TraceSinkError(f"{label} must be a JSON object")
    try:
        # Reuse the contract's recursive validation for JSON type and credential keys.
        canonical_json_bytes(payload)
    except (TypeError, ValueError) as exc:
        raise TraceSinkError(f"{label} is not safe JSON") from exc
    return payload


def _ensure_private_directory(path: Path) -> None:
    """Create a directory and enforce owner-only access, failing closed."""
    try:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise TraceSinkError("trace directory must be a real directory")
        path.chmod(0o700)
        if path.stat().st_mode & 0o777 != 0o700:
            raise TraceSinkError("platform cannot enforce private directory permissions")
    except TraceSinkError:
        raise
    except OSError as exc:
        raise TraceSinkError("unable to create private trace directory") from exc


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _check_report_root(report_root: Path | str) -> Path:
    root = Path(report_root)
    if not root.is_absolute():
        raise TraceSinkError("report root must be an absolute path")
    try:
        resolved = root.resolve(strict=False)
    except OSError as exc:
        raise TraceSinkError("unable to resolve report root") from exc
    if not _path_is_within(resolved, DEFAULT_REPORTS_ROOT):
        raise TraceSinkError("report root must be inside backend/data/rag/reports")
    return resolved


def _ensure_report_root(root: Path) -> None:
    """Create an absent reports root without following symlinks outside it."""
    base = DEFAULT_REPORTS_ROOT
    try:
        relative = root.relative_to(base)
    except ValueError as exc:
        raise TraceSinkError("report root must be inside backend/data/rag/reports") from exc
    _ensure_private_directory(base)
    current = base
    for component in relative.parts:
        current = current / component
        _ensure_private_directory(current)


def _safe_error_code(error_code: str) -> str:
    if not isinstance(error_code, str) or not _ERROR_CODE_PATTERN.fullmatch(error_code):
        raise TraceSinkError("failure code must be a stable lowercase classification")
    return error_code


class RagTraceSink:
    """Persist one immutable-path, private offline ``dev`` trace run.

    A run directory is created with exclusive semantics. Files are individually
    replaced atomically from temporary files in that same directory. Raw
    exception messages are never written to the trace; failures contain only a
    stable classification code.
    """

    def __init__(
        self,
        *,
        report_root: Path | str,
        dataset_name: str,
        run_id: UUID,
        expected_question_ids: Iterable[str],
        started_at: datetime,
        dataset: DataFingerprint,
        snapshot: DataFingerprint,
        implementation: dict[str, Any],
        configuration: dict[str, Any],
    ) -> None:
        self.report_root = _check_report_root(report_root)
        if not isinstance(dataset_name, str) or not _DATASET_NAME_PATTERN.fullmatch(dataset_name):
            raise TraceSinkError("dataset name must be a single safe path component")
        if not isinstance(run_id, UUID):
            raise TraceSinkError("run_id must be a UUID")
        if started_at.tzinfo is None or started_at.utcoffset() != timezone.utc.utcoffset(started_at):
            raise TraceSinkError("started_at must be timezone-aware UTC")

        question_ids = tuple(expected_question_ids)
        if not question_ids or any(not isinstance(value, str) or not value.strip() for value in question_ids):
            raise TraceSinkError("expected question IDs must be non-empty strings")
        if len(question_ids) != len(set(question_ids)):
            raise TraceSinkError("expected question IDs must be unique")
        # Validate the encoded file names before creating any state.
        self._question_files = {question_id: _safe_question_filename(question_id) for question_id in question_ids}

        self.dataset_name = dataset_name
        self.run_id = run_id
        self.expected_question_ids = question_ids
        self.started_at = started_at
        self.dataset = dataset
        self.snapshot = snapshot
        self.implementation = _validate_payload(implementation, label="implementation")
        self.configuration = _validate_payload(configuration, label="configuration")
        self._lock = threading.RLock()
        self._closed = False
        self._sequence = 0
        self._previous_event_sha256: str | None = None
        self._events: list[dict[str, Any]] = []
        self._written_questions: set[str] = set()
        self._reviews: dict[str, HumanAnswerReview] = {}
        self._failure_codes: list[str] = []

        date_component = started_at.astimezone(timezone.utc).date().isoformat()
        self.run_dir = (
            self.report_root
            / date_component
            / self.dataset_name
            / "dev"
            / "observe-v1"
            / str(self.run_id)
        )
        self.manifest_path = self.run_dir / "manifest.json"
        self.events_path = self.run_dir / "events.jsonl"
        self.reviews_path = self.run_dir / "human-reviews.jsonl"
        self.questions_dir = self.run_dir / "questions"
        self.failures_path = self.run_dir / "failures.json"

        _ensure_report_root(self.report_root)
        self._create_run_directory()
        try:
            _ensure_private_directory(self.questions_dir)
            self._atomic_write(self.run_dir / "run-metadata.json", self._metadata_bytes())
            self._atomic_write(self.events_path, b"")
            self._reviews = {
                question_id: HumanAnswerReview(
                    question_id=question_id,
                    reviewer_id=None,
                    reviewed_at=None,
                    correctness="not_reviewed",
                    completeness="not_reviewed",
                    groundedness="not_reviewed",
                    citation_fidelity="not_reviewed",
                    citation_chunk_ids=(),
                    rationale=None,
                )
                for question_id in self.expected_question_ids
            }
            self._write_reviews_file()
            self._write_manifest(status="running", finished_at=None)
        except Exception:
            self._record_failure("run_initialization_failed")
            raise

    def _create_run_directory(self) -> None:
        """Create each run component privately; final UUID mkdir is exclusive."""
        # report_root itself is explicitly selected by the caller. Newly-created
        # subdirectories below it are private, including all layout components.
        current = self.report_root
        relative = self.run_dir.relative_to(self.report_root)
        for component in relative.parts[:-1]:
            current = current / component
            _ensure_private_directory(current)
        try:
            self.run_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
            self.run_dir.chmod(0o700)
            if self.run_dir.stat().st_mode & 0o777 != 0o700:
                raise TraceSinkError("platform cannot enforce private run directory permissions")
        except FileExistsError as exc:
            raise TraceSinkError("run directory already exists; refusing to overwrite") from exc
        except TraceSinkError:
            raise
        except OSError as exc:
            raise TraceSinkError("unable to create private run directory") from exc

    def _metadata_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "trace_schema": TRACE_SCHEMA,
                "run_id": str(self.run_id),
                "dataset_name": self.dataset_name,
                "expected_question_ids": list(self.expected_question_ids),
            }
        )

    def _manifest(self, *, status: str, finished_at: datetime | None) -> TraceRunManifest:
        return TraceRunManifest(
            trace_schema=TRACE_SCHEMA,
            run_id=self.run_id,
            environment="dev",
            split="dev",
            started_at=self.started_at,
            finished_at=finished_at,
            status=status,
            dataset=self.dataset,
            snapshot=self.snapshot,
            implementation=self.implementation,
            configuration=self.configuration,
            artifacts=self._collect_artifacts(),
        )

    def _collect_artifacts(self) -> tuple[ArtifactDigest, ...]:
        artifacts: list[ArtifactDigest] = []
        for path in sorted(self.run_dir.rglob("*")):
            if path == self.manifest_path:
                continue
            if path.is_symlink():
                raise TraceIntegrityError("symlinks are not permitted in a trace run")
            if path.is_dir():
                continue
            if not path.is_file():
                raise TraceIntegrityError("trace artifact must be a regular file")
            relative_path = path.relative_to(self.run_dir).as_posix()
            content = path.read_bytes()
            artifacts.append(
                ArtifactDigest(
                    relative_path=relative_path,
                    size_bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            )
        return tuple(artifacts)

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        """Atomically replace one file using a same-directory 0600 temporary."""
        descriptor = -1
        temp_path: Path | None = None
        try:
            descriptor, temp_name = tempfile.mkstemp(prefix=".trace-write-", dir=path.parent)
            temp_path = Path(temp_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
            temp_path = None
            path.chmod(0o600)
            if path.stat().st_mode & 0o777 != 0o600:
                raise TraceSinkError("platform cannot enforce private file permissions")
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass

    def _write_manifest(self, *, status: str, finished_at: datetime | None) -> None:
        manifest = self._manifest(status=status, finished_at=finished_at)
        self._atomic_write(self.manifest_path, canonical_json_bytes(manifest))

    def _write_reviews_file(self) -> None:
        lines = [canonical_json_bytes(self._reviews[qid]) for qid in self.expected_question_ids]
        self._atomic_write(self.reviews_path, b"".join(line + b"\n" for line in lines))

    def _write_failures_file(self) -> None:
        content = canonical_json_bytes([{"code": code} for code in self._failure_codes])
        self._atomic_write(self.failures_path, content)

    def _record_failure(self, error_code: str) -> None:
        """Best-effort durable failure state while keeping raw errors private."""
        code = _safe_error_code(error_code)
        if self._closed:
            return
        if code not in self._failure_codes:
            self._failure_codes.append(code)
        try:
            self._write_failures_file()
            self._write_manifest(status="incomplete", finished_at=_utc_now())
        except Exception:
            # Preserve the original write/processing exception. Initialization
            # failures still fail closed because the caller receives the error.
            pass
        self._closed = True

    def _fail_for_write_error(self, error_code: str) -> None:
        try:
            self._record_failure(error_code)
        except Exception:
            self._closed = True

    def write_question(self, question_id: str, trace: dict[str, Any]) -> Path:
        with self._lock:
            self._ensure_open()
            self._validate_question_id(question_id)
            if question_id in self._written_questions:
                raise TraceSinkError("question trace already exists; refusing to overwrite")
            safe_trace = _validate_payload(trace, label="question trace")
            document = {
                "schema_version": TRACE_SCHEMA,
                "question_id": question_id,
                "trace": safe_trace,
            }
            content = canonical_json_bytes(document)
            path = self.questions_dir / self._question_files[question_id]
            try:
                self._atomic_write(path, content)
                self._written_questions.add(question_id)
                self._write_manifest(status="running", finished_at=None)
                return path
            except OSError:
                self._fail_for_write_error("artifact_write_failed")
                raise

    def append_event(
        self,
        *,
        question_id: str,
        event_type: str,
        status: str,
        payload: dict[str, Any],
        duration_ms: float | None = None,
        occurred_at: datetime | None = None,
    ) -> TraceEvent:
        with self._lock:
            self._ensure_open()
            self._validate_question_id(question_id)
            event_payload = _validate_payload(payload, label="event payload")
            next_sequence = self._sequence + 1
            values: dict[str, Any] = {
                "schema_version": TRACE_SCHEMA,
                "run_id": self.run_id,
                "question_id": question_id,
                "sequence": next_sequence,
                "occurred_at": occurred_at or _utc_now(),
                "event_type": event_type,
                "status": status,
                "duration_ms": duration_ms,
                "payload": event_payload,
                "previous_event_sha256": self._previous_event_sha256,
            }
            try:
                # Normalize UUID/datetime through the strict Pydantic contract
                # before canonical hashing, then hash exactly the serialized
                # public event fields.
                provisional = TraceEvent(**values, event_sha256="0" * 64)
                normalized = provisional.model_dump(mode="json", exclude={"event_sha256"})
                digest = canonical_json_sha256(normalized)
                event = TraceEvent(**values, event_sha256=digest)
                event_json = event.model_dump(mode="json")
                updated_events = [*self._events, event_json]
                event_content = b"".join(
                    canonical_json_bytes(item) + b"\n" for item in updated_events
                )
                self._atomic_write(self.events_path, event_content)
                self._events = updated_events
                self._sequence = next_sequence
                self._previous_event_sha256 = digest
                self._write_manifest(status="running", finished_at=None)
                return event
            except (OSError, TraceSinkError):
                self._fail_for_write_error("event_write_failed")
                raise

    def write_review(self, review: HumanAnswerReview) -> None:
        with self._lock:
            self._ensure_open()
            self._validate_question_id(review.question_id)
            try:
                self._reviews[review.question_id] = review
                self._write_reviews_file()
                self._write_manifest(status="running", finished_at=None)
            except OSError:
                self._fail_for_write_error("review_write_failed")
                raise

    def write_artifact(self, relative_path: str, content: bytes | str) -> Path:
        """Write one additional safe JSON object as a private run artifact."""
        with self._lock:
            self._ensure_open()
            if not isinstance(relative_path, str) or not relative_path.strip():
                raise TraceSinkError("artifact path must be a non-empty relative path")
            artifact_path = PurePosixPath(relative_path)
            if (
                artifact_path.is_absolute()
                or ".." in artifact_path.parts
                or "\\" in relative_path
                or len(artifact_path.parts) != 1
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", relative_path)
                or relative_path in {
                    "manifest.json",
                    "events.jsonl",
                    "human-reviews.jsonl",
                    "run-metadata.json",
                    "failures.json",
                }
            ):
                raise TraceSinkError("artifact path must be a safe top-level run file")
            path = self.run_dir / relative_path
            if path.exists() or path.is_symlink():
                raise TraceSinkError("artifact already exists; refusing to overwrite")
            try:
                payload = content.encode("utf-8") if isinstance(content, str) else content
                if not isinstance(payload, bytes):
                    raise TraceSinkError("artifact content must be bytes or text")
                try:
                    decoded = json.loads(payload)
                    _validate_payload(decoded, label="artifact")
                except (UnicodeDecodeError, json.JSONDecodeError, TraceSinkError) as exc:
                    raise TraceSinkError("artifact must contain a safe JSON object") from exc
                self._atomic_write(path, payload)
                self._write_manifest(status="running", finished_at=None)
                return path
            except OSError:
                self._fail_for_write_error("artifact_write_failed")
                raise

    def mark_incomplete(self, error_code: str) -> TraceRunManifest:
        with self._lock:
            if self._closed:
                manifest = TraceRunManifest.model_validate_json(self.manifest_path.read_bytes())
                if manifest.status != "complete":
                    return manifest
                # A caller may perform an additional validation after complete()
                # (for example, checking the final report artifact). Permit that
                # validation failure to downgrade the run without replacing any
                # existing question or event artifact.
                self._closed = False
                failures_path = self.failures_path
                if failures_path.is_file():
                    try:
                        failures = json.loads(failures_path.read_bytes())
                    except (OSError, json.JSONDecodeError):
                        failures = []
                    self._failure_codes = [
                        item["code"]
                        for item in failures
                        if isinstance(item, dict) and isinstance(item.get("code"), str)
                    ]
            self._record_failure(error_code)
            return TraceRunManifest.model_validate_json(self.manifest_path.read_bytes())

    def complete(self) -> TraceRunManifest:
        with self._lock:
            self._ensure_open()
            try:
                self._validate_completion_conditions()
                self._write_reviews_file()
                self._write_manifest(status="complete", finished_at=_utc_now())
                verified = verify_trace_run(self.run_dir)
                self._closed = True
                return verified
            except Exception:
                self._fail_for_write_error("completion_validation_failed")
                raise

    def _validate_completion_conditions(self) -> None:
        missing_questions = set(self.expected_question_ids) - self._written_questions
        if missing_questions:
            raise TraceIntegrityError("expected question artifacts are missing")
        event_question_ids = {event["question_id"] for event in self._events}
        missing_events = set(self.expected_question_ids) - event_question_ids
        if missing_events:
            raise TraceIntegrityError("expected question event traces are missing")
        if len(self._written_questions) != len(self.expected_question_ids):
            raise TraceIntegrityError("question count does not match expected dev split size")

    def _validate_question_id(self, question_id: str) -> None:
        if question_id not in self._question_files:
            raise TraceSinkError("unknown question id; it is not part of the expected dev split")

    def _ensure_open(self) -> None:
        if self._closed:
            raise TraceSinkError("trace run is already closed")

    def __enter__(self) -> RagTraceSink:
        self._ensure_open()
        return self

    def __exit__(self, exception_type, exception, traceback) -> bool:
        if exception_type is not None:
            self._fail_for_write_error("evaluation_error")
            return False
        if not self._closed:
            self.complete()
        return False


def _load_manifest(run_dir: Path) -> TraceRunManifest:
    try:
        return TraceRunManifest.model_validate_json((run_dir / "manifest.json").read_bytes())
    except (OSError, ValidationError, ValueError) as exc:
        raise TraceIntegrityError("manifest is missing or invalid") from exc


def _listed_artifact_paths(manifest: TraceRunManifest) -> set[str]:
    return {artifact.relative_path for artifact in manifest.artifacts}


def _actual_artifact_paths(run_dir: Path) -> set[str]:
    paths: set[str] = set()
    for path in run_dir.rglob("*"):
        if path.is_symlink():
            raise TraceIntegrityError("symlinks are not permitted in a trace run")
        if path.is_dir():
            continue
        if not path.is_file():
            raise TraceIntegrityError("trace artifact must be a regular file")
        relative = path.relative_to(run_dir).as_posix()
        if relative != "manifest.json":
            paths.add(relative)
    return paths


def _verify_event_chain(
    run_dir: Path,
    manifest: TraceRunManifest,
    expected_question_ids: set[str],
) -> set[str]:
    event_path = run_dir / "events.jsonl"
    if not event_path.exists():
        raise TraceIntegrityError("events.jsonl artifact is missing")
    raw = event_path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise TraceIntegrityError("event JSONL must end with a newline")
    previous_hash: str | None = None
    question_ids: set[str] = set()
    expected_sequence = 1
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line:
            raise TraceIntegrityError("event JSONL contains an empty line")
        try:
            event = TraceEvent.model_validate_json(line)
        except (ValidationError, ValueError) as exc:
            raise TraceIntegrityError(f"event {line_number} has invalid schema") from exc
        if event.schema_version != TRACE_SCHEMA or event.run_id != manifest.run_id:
            raise TraceIntegrityError("event schema or run id does not match manifest")
        if event.question_id not in expected_question_ids:
            raise TraceIntegrityError("event references a question outside the expected dev split")
        if event.sequence != expected_sequence:
            raise TraceIntegrityError("event sequence is not strictly increasing")
        if event.previous_event_sha256 != previous_hash:
            raise TraceIntegrityError("event hash chain previous digest mismatch")
        values = event.model_dump(mode="json", exclude={"event_sha256"})
        if canonical_json_sha256(values) != event.event_sha256:
            raise TraceIntegrityError("event hash does not match canonical event content")
        previous_hash = event.event_sha256
        question_ids.add(event.question_id)
        expected_sequence += 1
    return question_ids


def verify_trace_run(run_dir: Path | str) -> TraceRunManifest:
    """Verify manifest, event chain, question count, and every artifact digest."""
    directory = Path(run_dir)
    if not directory.is_absolute() or not directory.is_dir() or directory.is_symlink():
        raise TraceIntegrityError("run directory must be an existing absolute real directory")
    try:
        resolved = directory.resolve(strict=True)
    except OSError as exc:
        raise TraceIntegrityError("run directory cannot be resolved") from exc
    if not _path_is_within(resolved, DEFAULT_REPORTS_ROOT):
        raise TraceIntegrityError("run directory is outside backend/data/rag/reports")
    if directory.stat().st_mode & 0o777 != 0o700:
        raise TraceIntegrityError("run directory permissions are not private")

    manifest = _load_manifest(directory)
    if manifest.trace_schema != TRACE_SCHEMA or manifest.environment != "dev" or manifest.split != "dev":
        raise TraceIntegrityError("manifest schema or split is not an approved dev trace")

    listed = _listed_artifact_paths(manifest)
    actual = _actual_artifact_paths(directory)
    if listed != actual:
        raise TraceIntegrityError("manifest artifact set does not match files on disk")
    for artifact in manifest.artifacts:
        path = directory / artifact.relative_path
        try:
            path.resolve(strict=True).relative_to(resolved)
        except (OSError, ValueError) as exc:
            raise TraceIntegrityError("artifact path escapes run directory") from exc
        if path.stat().st_mode & 0o777 != 0o600:
            raise TraceIntegrityError("artifact file permissions are not private")
        content = path.read_bytes()
        if len(content) != artifact.size_bytes:
            raise TraceIntegrityError(f"artifact size mismatch: {artifact.relative_path}")
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise TraceIntegrityError(f"artifact digest mismatch: {artifact.relative_path}")
        try:
            canonical_json_bytes(json.loads(content))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            # JSONL artifacts consist of one canonical JSON object per line.
            if artifact.relative_path not in {"events.jsonl", "human-reviews.jsonl"}:
                raise TraceIntegrityError(f"artifact is not valid safe JSON: {artifact.relative_path}") from exc
            try:
                for line in content.splitlines():
                    canonical_json_bytes(json.loads(line))
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as line_exc:
                raise TraceIntegrityError(f"artifact is not valid safe JSONL: {artifact.relative_path}") from line_exc

    metadata_path = directory / "run-metadata.json"
    try:
        metadata = json.loads(metadata_path.read_bytes())
        expected_ids = metadata["expected_question_ids"]
        if (
            metadata["trace_schema"] != TRACE_SCHEMA
            or UUID(metadata["run_id"]) != manifest.run_id
            or not isinstance(expected_ids, list)
            or not expected_ids
            or len(expected_ids) != len(set(expected_ids))
            or any(not isinstance(question_id, str) for question_id in expected_ids)
        ):
            raise ValueError("metadata contract mismatch")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TraceIntegrityError("run metadata is invalid") from exc

    question_dir = directory / "questions"
    question_files = {
        path.name for path in question_dir.glob("*.json") if path.is_file() and not path.is_symlink()
    }
    expected_files = {_safe_question_filename(question_id) for question_id in expected_ids}
    if question_files - expected_files:
        raise TraceIntegrityError("question artifacts include unknown question IDs")
    if manifest.status == "complete" and question_files != expected_files:
        raise TraceIntegrityError("question artifact count does not match expected dev split size")
    question_ids_to_validate = (
        expected_ids
        if manifest.status == "complete"
        else [
            question_id
            for question_id in expected_ids
            if _safe_question_filename(question_id) in question_files
        ]
    )
    for question_id in question_ids_to_validate:
        path = question_dir / _safe_question_filename(question_id)
        try:
            question = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            raise TraceIntegrityError("question artifact is invalid JSON") from exc
        if (
            question.get("schema_version") != TRACE_SCHEMA
            or question.get("question_id") != question_id
            or not isinstance(question.get("trace"), dict)
        ):
            raise TraceIntegrityError("question artifact schema does not match expected question")

    event_question_ids = _verify_event_chain(directory, manifest, set(expected_ids))
    if manifest.status == "complete":
        if manifest.finished_at is None:
            raise TraceIntegrityError("complete run must have finished_at")
        if set(expected_ids) - event_question_ids:
            raise TraceIntegrityError("complete run lacks question event coverage")
        if question_files != expected_files:
            raise TraceIntegrityError("complete run question count mismatch")
    elif manifest.finished_at is None:
        if manifest.status != "running":
            raise TraceIntegrityError("terminal run must have finished_at")
    elif manifest.status == "running":
        raise TraceIntegrityError("running run must not have finished_at")

    reviews_path = directory / "human-reviews.jsonl"
    review_lines = reviews_path.read_bytes().splitlines()
    if len(review_lines) != len(expected_ids):
        raise TraceIntegrityError("human review count does not match expected question count")
    review_ids: list[str] = []
    for line in review_lines:
        try:
            review = HumanAnswerReview.model_validate_json(line)
        except (ValidationError, ValueError) as exc:
            raise TraceIntegrityError("human review record is invalid") from exc
        review_ids.append(review.question_id)
    if review_ids != expected_ids:
        raise TraceIntegrityError("human review records do not match expected question order")

    if manifest.status == "incomplete":
        failures_path = directory / "failures.json"
        if not failures_path.is_file():
            raise TraceIntegrityError("incomplete run lacks failure classification")
        try:
            failures = json.loads(failures_path.read_bytes())
            if not isinstance(failures, list) or not failures:
                raise ValueError("empty failure list")
            if any(
                not isinstance(item, dict)
                or set(item) != {"code"}
                or not isinstance(item["code"], str)
                or not _ERROR_CODE_PATTERN.fullmatch(item["code"])
                for item in failures
            ):
                raise ValueError("invalid failure classification")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise TraceIntegrityError("incomplete run failure classification is invalid") from exc
    return manifest