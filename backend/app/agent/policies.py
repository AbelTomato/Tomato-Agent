import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent.harness_models import ToolPolicy
from app.errors import (
    ToolInputError as LegacyToolInputError,
    ToolNotFoundError,
    ToolTimeoutError,
)
from app.tools.base import ToolResult


Capability = Literal[
    "file", "process", "network", "credential", "resource", "side_effect"
]
SideEffect = Literal["none", "read", "write", "unknown"]
ErrorCode = Literal[
    "unknown_tool",
    "invalid_arguments",
    "permission_denied",
    "timeout",
    "output_limit",
    "execution_failed",
]


class _PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ToolDeclaration(_PolicyModel):
    name: str = Field(min_length=1)
    description: str
    input_schema: dict[str, Any]
    capabilities: frozenset[Capability] = frozenset()
    side_effect: SideEffect = "unknown"


class CapabilityProfile(_PolicyModel):
    allowed_paths: tuple[str, ...] = ()
    allow_process: bool = False
    allow_network: bool = False
    credential_names: tuple[str, ...] = ()
    timeout_seconds: float = Field(default=20.0, gt=0)
    max_output_chars: int = Field(default=10_000, gt=0)

    @field_validator("allowed_paths")
    @classmethod
    def absolute_paths_only(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not path.startswith("/") or path == "/" for path in value):
            raise ValueError("capability paths must be non-root absolute paths")
        return value

    @field_validator("credential_names")
    @classmethod
    def credentials_are_disabled(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value:
            raise ValueError("credential access is disabled by default")
        return value

    @model_validator(mode="after")
    def deny_unsafe_capabilities(self) -> "CapabilityProfile":
        if self.allow_process or self.allow_network:
            raise ValueError("process and network capabilities require explicit sandbox approval")
        return self


CODE_TASK_TOOLS = frozenset({
    "list_files", "read_file", "search_files", "write_file", "apply_patch",
    "run_tests", "get_diff", "collect_artifact",
})


class CodeTaskCapabilityProfile(CapabilityProfile):
    """Server-owned contract; does not authorize host process execution."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    allowed_tools: frozenset[str] = CODE_TASK_TOOLS
    sandbox_backend: Literal["local", "docker", "openshell"] = "local"

    @field_validator("allowed_tools")
    @classmethod
    def registered_tools_only(cls, value: frozenset[str]) -> frozenset[str]:
        if not value <= CODE_TASK_TOOLS:
            raise ValueError("code tasks only allow registered code tools")
        return value


class ToolExecutionError(Exception):
    def __init__(self, code: ErrorCode, public_message: str, *, retryable: bool = False):
        super().__init__(public_message)
        self.code = code
        self.retryable = retryable
        self.public_message = public_message


def _error(code: ErrorCode, message: str, *, retryable: bool = False) -> ToolExecutionError:
    return ToolExecutionError(code, message, retryable=retryable)


def _schema_matches(value: Any, schema: dict[str, Any]) -> bool:
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return False
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", ())):
            return False
        if schema.get("additionalProperties") is False and any(
            key not in properties for key in value
        ):
            return False
        return all(
            key not in properties or _schema_matches(item, properties[key])
            for key, item in value.items()
        )
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    return True


def validate_tool_call(
    declaration: ToolDeclaration, arguments: dict[str, Any], policy: ToolPolicy
) -> None:
    if declaration.name not in policy.allowed_tools:
        raise _error("unknown_tool", "The requested tool is not allowlisted.")
    if declaration.side_effect in {"write", "unknown"} or declaration.capabilities & {
        "process", "network", "credential", "side_effect"
    }:
        raise _error("permission_denied", "The tool capability is not approved.")
    if not _schema_matches(arguments, declaration.input_schema):
        raise _error("invalid_arguments", "The tool arguments are invalid.")


def validate_capability_profile(profile: CapabilityProfile) -> None:
    if profile.allow_process or profile.allow_network or profile.credential_names:
        raise _error("permission_denied", "The capability profile is not safely restricted.")
    if any(not path.startswith("/") or path == "/" for path in profile.allowed_paths):
        raise _error("permission_denied", "The capability profile contains an unsafe path.")


def map_tool_exception(exc: BaseException) -> ToolExecutionError:
    if isinstance(exc, (ToolNotFoundError,)):
        return _error("unknown_tool", "The requested tool is not available.")
    if isinstance(exc, (LegacyToolInputError, ValueError, TypeError)):
        return _error("invalid_arguments", "The tool arguments are invalid.")
    if isinstance(exc, (ToolTimeoutError, TimeoutError)):
        return _error("timeout", "The tool exceeded its time limit.", retryable=True)
    return _error("execution_failed", "The tool failed to execute safely.")


def map_tool_result(
    result: ToolResult, *, max_output_chars: int | None = None
) -> ToolExecutionError | None:
    if not result.success:
        return _error("execution_failed", "The tool returned an execution failure.")
    if max_output_chars is not None:
        rendered = json.dumps(result.data, ensure_ascii=False, default=str)
        if len(rendered) > max_output_chars:
            return _error("output_limit", "The tool result exceeded the output limit.")
    return None