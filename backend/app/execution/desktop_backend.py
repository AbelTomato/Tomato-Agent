import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator


MAX_ARGV_BYTES = 64 * 1024
MAX_ENVIRONMENT_BYTES = 64 * 1024
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


EXECUTION_BACKEND_PROTOCOL_VERSION = 1


class ExecutionStatus(StrEnum):
    COMPLETED = "completed"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    FAILED = "failed"
    UNKNOWN = "unknown"


class BackendCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    protocol_version: Literal[1] = EXECUTION_BACKEND_PROTOCOL_VERSION
    backend_id: str
    platform: str
    command_execution: bool
    network_access: bool
    workspace_writeback: bool
    supports_cancel: bool
    supports_inspect: bool


class ExecutionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    network_enabled: bool = False
    timeout_limit_seconds: int = Field(default=300, ge=1, le=3600)
    output_limit_bytes: int = Field(default=256_000, ge=1, le=4_000_000)
    environment_allowlist: tuple[str, ...] = Field(default=(), max_length=128)

    @field_validator("environment_allowlist")
    @classmethod
    def validate_environment_allowlist(cls, names):
        if len(names) != len(set(names)) or any(
            len(name) > 128 or not _ENVIRONMENT_NAME.fullmatch(name) for name in names
        ):
            raise ValueError("environment allowlist must contain unique variable names")
        return names


class WorkspaceSnapshot(BaseModel):
    """Immutable, content-addressed input; backends must never write to its source."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    snapshot_id: str = Field(min_length=1, max_length=200)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExecutionHandle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    handle_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    handle_id: str = Field(min_length=1, max_length=200)
    invocation_id: str = Field(min_length=1, max_length=200)
    argv: tuple[str, ...] = Field(min_length=1, max_length=256)
    cwd: str = Field(min_length=1, max_length=32_768)
    environment: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(ge=1, le=3600)

    @field_validator("argv")
    @classmethod
    def require_nonempty_arguments(cls, argv):
        if any(not argument or "\x00" in argument for argument in argv):
            raise ValueError("argv entries must be non-empty and contain no NUL")
        if sum(len(argument.encode("utf-8")) for argument in argv) > MAX_ARGV_BYTES:
            raise ValueError("argv exceeds the request byte limit")
        return argv

    @field_validator("environment")
    @classmethod
    def bound_environment(cls, environment):
        total = 0
        for name, value in environment.items():
            if (len(name) > 128 or not _ENVIRONMENT_NAME.fullmatch(name) or "\x00" in value):
                raise ValueError("invalid environment entry")
            total += len(name.encode("utf-8")) + len(value.encode("utf-8"))
        if len(environment) > 128 or total > MAX_ENVIRONMENT_BYTES:
            raise ValueError("environment exceeds the request byte limit")
        return environment

    @field_validator("cwd")
    @classmethod
    def require_workspace_relative_cwd(cls, cwd):
        path = PurePosixPath(cwd)
        if "\\" in cwd or "\x00" in cwd or path.is_absolute() or ".." in path.parts:
            raise ValueError("cwd must stay within the workspace")
        if len(cwd.encode("utf-8")) > 32_768:
            raise ValueError("cwd exceeds the request byte limit")
        return cwd


class BackendOperationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: ExecutionStatus
    error_code: str | None = None
    handle: ExecutionHandle | None = None
    detail: str | None = None


class ExecutionResult(BackendOperationResult):
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    output_truncated: bool = False


class ExecutionObservation(BackendOperationResult):
    observed_status: ExecutionStatus | None = None


class ChangeCollectionResult(BackendOperationResult):
    changeset_id: str | None = None


class ExecutionBackend(Protocol):
    """Executor contract: materialize an isolated copy of the immutable snapshot.

    Implementations must not mount or mutate the registered project root. Network,
    environment, timeout, output, cancellation, inspection, and cleanup outcomes
    are all constrained by the supplied policy and invocation identity.
    """

    async def describe_capabilities(self) -> BackendCapabilities: ...

    async def prepare(
        self,
        run_id: str,
        workspace_snapshot: WorkspaceSnapshot,
        policy: ExecutionPolicy,
    ) -> BackendOperationResult: ...

    async def execute(self, request: ExecutionRequest) -> ExecutionResult: ...

    async def cancel(self, handle_id: str, invocation_id: str) -> BackendOperationResult: ...

    async def inspect(self, handle_id: str, invocation_id: str) -> ExecutionObservation: ...

    async def collect_changes(self, handle_id: str) -> ChangeCollectionResult: ...

    async def close(self, handle_id: str) -> BackendOperationResult: ...


class ExecutionBackendUnavailable:
    code = "backend_unavailable"


class UnavailableExecutionBackend:
    """First-version backend: advertises no execution and never falls back to host."""

    @staticmethod
    def _rejected(result_type=BackendOperationResult):
        return result_type(status=ExecutionStatus.REJECTED,
                           error_code=ExecutionBackendUnavailable.code)

    async def describe_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            backend_id="unavailable",
            platform="none",
            command_execution=False,
            network_access=False,
            workspace_writeback=False,
            supports_cancel=False,
            supports_inspect=False,
        )

    async def prepare(self, run_id, workspace_snapshot, policy) -> BackendOperationResult:
        return self._rejected()

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return self._rejected(ExecutionResult)

    async def cancel(self, handle_id: str, invocation_id: str) -> BackendOperationResult:
        return self._rejected()

    async def inspect(self, handle_id: str, invocation_id: str) -> ExecutionObservation:
        return self._rejected(ExecutionObservation)

    async def collect_changes(self, handle_id: str) -> ChangeCollectionResult:
        return self._rejected(ChangeCollectionResult)

    async def close(self, handle_id: str) -> BackendOperationResult:
        return self._rejected()
