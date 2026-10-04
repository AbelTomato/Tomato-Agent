"""Failure-closed sandbox execution contracts.

This module deliberately does not execute host commands.  A local backend may be
injected for deterministic tests, while the default executor rejects requests
when no isolation-capable backend is configured.
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from pydantic_core import ValidationError

from app.agent.policies import CapabilityProfile, validate_capability_profile


class SandboxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any]
    input_refs: tuple[str, ...] = ()
    request_id: str = Field(min_length=1)


class SandboxResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["completed", "rejected", "timed_out", "cancelled", "failed"]
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    output_truncated: bool = False
    error_code: str | None = None


class SandboxBackend(Protocol):
    def __call__(
        self, request: SandboxRequest, profile: CapabilityProfile
    ) -> Any | Awaitable[Any]: ...


def _rejected(code: str) -> SandboxResult:
    return SandboxResult(status="rejected", error_code=code)


class SandboxExecutor(Protocol):
    async def execute(
        self, request: SandboxRequest, profile: CapabilityProfile
    ) -> SandboxResult: ...


class LocalSandboxExecutor:
    """Run only an explicitly injected fake/local backend.

    The default constructor has no backend and therefore always rejects.  It
    never interprets a request as a shell command and never falls back to the
    host operating system.
    """

    def __init__(self, backend: SandboxBackend | None = None):
        self._backend = backend

    async def execute(
        self, request: SandboxRequest, profile: CapabilityProfile
    ) -> SandboxResult:
        try:
            validate_capability_profile(profile)
        except (AttributeError, TypeError, ValueError, ValidationError):
            return _rejected("invalid_profile")

        if self._backend is None:
            return _rejected("backend_unavailable")

        try:
            result = self._backend(request, profile)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, profile.timeout_seconds)
            return self._normalize_result(result, profile.max_output_chars)
        except asyncio.CancelledError:
            return SandboxResult(status="cancelled", error_code="cancelled")
        except asyncio.TimeoutError:
            return SandboxResult(status="timed_out", error_code="timeout")
        except Exception:
            return SandboxResult(status="failed", error_code="execution_failed")

    @staticmethod
    def _normalize_result(result: Any, max_output_chars: int) -> SandboxResult:
        if isinstance(result, SandboxResult):
            raw = result.model_dump()
        elif isinstance(result, dict):
            raw = dict(result)
        else:
            raise TypeError("sandbox backend returned an invalid result")

        stdout = raw.get("stdout", "")
        stderr = raw.get("stderr", "")
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            raise TypeError("sandbox output must be text")

        combined = stdout + stderr
        truncated = len(combined) > max_output_chars
        if truncated:
            stdout = stdout[:max_output_chars]
            stderr = ""

        status = raw.get("status", "completed")
        if status != "completed":
            raise ValueError("local backend may only return completed output")
        return SandboxResult(
            status="completed",
            exit_code=raw.get("exit_code"),
            stdout=stdout,
            stderr=stderr,
            output_truncated=truncated or bool(raw.get("output_truncated", False)),
            error_code=None,
        )


class OpenShellSandboxExecutor:
    """Contract-only adapter; intentionally unavailable in this phase."""

    async def execute(
        self, request: SandboxRequest, profile: CapabilityProfile
    ) -> SandboxResult:
        return _rejected("backend_unavailable")