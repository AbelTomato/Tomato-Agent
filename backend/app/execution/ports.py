from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from typing import Protocol
from uuid import uuid4

from .models import ExecutionSnapshot, LeaseHandle, OperationInput, StepOutcome


class ExecutionPort(Protocol):
    lease: LeaseHandle
    workspace_id: str

    async def save_snapshot(self, snapshot: ExecutionSnapshot) -> ExecutionSnapshot: ...
    async def describe_operation(self, logical_index, tool_name, arguments) -> OperationInput: ...
    async def commit_model_response(self, response, snapshot, operation=None) -> ExecutionSnapshot: ...
    async def get_operation(self, operation_id): ...
    async def start_step(self, operation_id, snapshot): ...
    async def commit_step(self, step_id, result, snapshot) -> ExecutionSnapshot: ...


class WorkerRepository(Protocol):
    async def claim_next(self, owner_id: str, *, lease_seconds: int) -> tuple[object, LeaseHandle] | None: ...
    async def heartbeat(self, lease: LeaseHandle, *, lease_seconds: int) -> LeaseHandle: ...
    async def scan_expired(self, limit: int) -> list[object]: ...
    async def expire_and_classify(self, attempt_id): ...
    async def get_recovery_candidate(self, run_id): ...
    async def apply_recovery(self, candidate, decision): ...


class LeaseExecutionPort:
    """Lease-bound persistence; side-effect metadata must be supplied by an adapter."""

    def __init__(self, repository, lease: LeaseHandle, workspace_id: str):
        self.repository = repository
        self.lease = lease
        self.workspace_id = workspace_id

    async def save_snapshot(self, snapshot):
        return await self.repository.save_snapshot(self.lease, snapshot)

    async def heartbeat(self, *, lease_seconds):
        self.lease = await self.repository.heartbeat(self.lease, lease_seconds=lease_seconds)
        return self.lease

    async def finish(self, status, **kwargs):
        return await self.repository.finish(self.lease, status, **kwargs)

    async def describe_operation(self, logical_index, tool_name, arguments):
        # Unknown tools are conservatively treated as writes, never safe reads.
        return OperationInput(id=uuid4(), logical_index=logical_index, kind="tool",
                              tool_name=tool_name, input_payload=arguments, recovery_class="file_write")

    async def commit_model_response(self, response, snapshot, operation=None):
        return await self.repository.commit_model_response(self.lease, response, snapshot, operation)

    async def get_operation(self, operation_id):
        return await self.repository.get_operation(operation_id)

    async def start_step(self, operation_id, snapshot):
        return await self.repository.start_step(self.lease, operation_id, snapshot)

    async def commit_step(self, step_id, result, snapshot):
        return await self.repository.commit_step(self.lease, step_id, result, snapshot)


class Clock(Protocol):
    def now(self) -> datetime:
        """Return a timezone-aware datetime."""


class Migration(Protocol):
    version: int
    statements: tuple[str, ...]


def execution_migration(version: int = 1) -> tuple[str, ...]:
    """Return the idempotent v1 execution schema without performing I/O."""

    if version != 1:
        raise ValueError(f"unsupported execution schema version: {version}")
    return (
        """
        CREATE TABLE IF NOT EXISTS execution_run_heads (
            run_id TEXT PRIMARY KEY REFERENCES task_runs(id),
            active_attempt_id TEXT,
            fencing_token INTEGER NOT NULL CHECK (fencing_token >= 0),
            cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
            protocol_version INTEGER NOT NULL
        )
        """.strip(),
        """
        CREATE TABLE IF NOT EXISTS execution_attempts (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES task_runs(id),
            attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
            owner_id TEXT NOT NULL,
            fencing_token INTEGER NOT NULL CHECK (fencing_token >= 0),
            status TEXT NOT NULL,
            lease_expires_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            error_code TEXT,
            UNIQUE (run_id, attempt_no),
            UNIQUE (run_id, fencing_token)
        )
        """.strip(),
        "CREATE INDEX IF NOT EXISTS idx_execution_attempts_lease ON execution_attempts(status, lease_expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_execution_attempts_run ON execution_attempts(run_id)",
    )


def canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()