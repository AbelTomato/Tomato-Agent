"""Registered test targets dispatched exclusively through the sandbox contract."""

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.agent.models import ToolDefinition
from app.agent.policies import CodeTaskCapabilityProfile, ToolDeclaration
from app.agent.sandbox import SandboxExecutor, SandboxRequest, SandboxResult
from app.artifacts.service import ArtifactService
from app.runs.repository import RunRepository
from app.workspaces.service import WorkspacePathError, WorkspaceService

from .base import ToolContext, ToolResult


class RegisteredTestTarget(BaseModel):
    """Immutable server configuration. Command is argv, never shell source."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: str = Field(min_length=1)
    command: tuple[str, ...] = Field(min_length=1)
    cwd: str = ""
    allowed_arguments: tuple[str, ...] = ()

    @field_validator("cwd")
    @classmethod
    def relative_directory(cls, value: str) -> str:
        if Path(value).is_absolute() or ".." in Path(value).parts or "\x00" in value:
            raise ValueError("Test cwd must be workspace-relative")
        return value

    @field_validator("command", "allowed_arguments")
    @classmethod
    def valid_tokens(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not token or "\x00" in token for token in value):
            raise ValueError("Invalid argv token")
        return value


class TestTargetRegistry:
    __test__ = False

    def __init__(self, targets: list[RegisteredTestTarget]):
        self._targets = {}
        for target in targets:
            if target.name in self._targets:
                raise ValueError("Duplicate test target")
            self._targets[target.name] = target

    def resolve(self, target: str) -> RegisteredTestTarget:
        try:
            return self._targets[target]
        except KeyError as exc:
            raise ValueError("Unknown test target") from exc


class RunTestsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    target: str = Field(min_length=1)
    arguments: list[str] = Field(default_factory=list, max_length=100)


class RunTestsTool:
    name = "run_tests"
    description = "Run a server-registered test target in the authorized sandbox."
    input_model = RunTestsArguments

    def __init__(self, targets: TestTargetRegistry, executor: SandboxExecutor,
                 workspaces: WorkspaceService, artifacts: ArtifactService,
                 runs: RunRepository):
        self.targets = targets
        self.executor = executor
        self.workspaces = workspaces
        self.artifacts = artifacts
        self.runs = runs

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description=self.description,
                              parameters=self.input_model.model_json_schema())

    def declaration(self) -> ToolDeclaration:
        return ToolDeclaration(
            name=self.name, description=self.description,
            input_schema=self.input_model.model_json_schema(),
            capabilities=frozenset({"file", "process", "resource"}), side_effect="write",
        )

    @staticmethod
    def _bounded(data: dict, limit: int) -> ToolResult:
        data["truncated"] = False
        while len(json.dumps(data, ensure_ascii=False)) > limit:
            data["truncated"] = True
            key = next((key for key in ("stdout", "stderr") if data.get(key)), None)
            if key is None:
                return ToolResult(success=False, error="output_limit")
            excess = len(json.dumps(data, ensure_ascii=False)) - limit
            data[key] = data[key][:max(0, len(data[key]) - max(1, excess))]
        return ToolResult(success=True, data=data)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            args = self.input_model.model_validate(arguments)
            target = self.targets.resolve(args.target)
            if any(argument not in target.allowed_arguments for argument in args.arguments):
                raise ValueError("Unregistered test argument")
            if not isinstance(context.profile, CodeTaskCapabilityProfile):
                raise PermissionError
            profile = CodeTaskCapabilityProfile.model_validate(context.profile.model_dump())
            if not context.workspace_id or self.name not in profile.allowed_tools:
                raise PermissionError
            run = await self.runs.get_run(context.run_id)
            if run is None or run.workspace_id != context.workspace_id:
                raise PermissionError
            cwd = self.workspaces.resolve(context.workspace_id, target.cwd)
            if not cwd.is_dir() or not any(
                cwd.is_relative_to(Path(root).resolve()) for root in profile.allowed_paths
            ):
                raise PermissionError
            request = SandboxRequest(
                tool_name=self.name, request_id=str(uuid4()),
                input_refs=(context.run_id, context.workspace_id),
                arguments={"target": target.name, "argv": [*target.command, *args.arguments],
                           "cwd": str(cwd)},
            )
            try:
                result = await self.executor.execute(request, profile)
            except asyncio.CancelledError:
                result = SandboxResult(status="cancelled", error_code="cancelled")
            except TimeoutError:
                result = SandboxResult(status="timed_out", error_code="timeout")
            except Exception:
                result = SandboxResult(status="failed", error_code="execution_failed")
            # Enforce output bounds even for injected adapters returning oversized text.
            stdout = result.stdout[:profile.max_output_chars]
            stderr = result.stderr[:max(0, profile.max_output_chars - len(stdout))]
            report = {
                "target": target.name, "run_id": context.run_id,
                "workspace_id": context.workspace_id, "status": result.status,
                "exit_code": result.exit_code,
                "passed": result.status == "completed" and result.exit_code == 0,
                "stdout": stdout, "stderr": stderr,
                "output_truncated": result.output_truncated or (
                    len(result.stdout) + len(result.stderr) > profile.max_output_chars),
                "error_code": result.error_code,
            }
            ref = await self.artifacts.register_text(
                context.run_id, "test_report", json.dumps(report, ensure_ascii=False))
            await self.workspaces.record_artifact_ref(
                context.workspace_id, ref.model_dump(mode="json"))
            data = {key: value for key, value in report.items()
                    if key not in {"run_id", "workspace_id"}}
            data["artifact"] = ref.model_dump(mode="json")
            bounded = self._bounded(data, profile.max_output_chars)
            if result.status != "completed" and bounded.success:
                bounded.success = False
                bounded.error = result.error_code or result.status
            return bounded
        except (PermissionError, WorkspacePathError):
            return ToolResult(success=False, error="permission_denied")
        except (ValidationError, ValueError, TypeError):
            return ToolResult(success=False, error="invalid_arguments")
        except (KeyError, OSError):
            return ToolResult(success=False, error="execution_failed")