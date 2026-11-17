from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.models import ToolDefinition
from app.assistant.models import CapabilityDescriptor, CapabilityError, CapabilityOutcome
from app.execution.desktop_backend import ExecutionBackend, ExecutionBackendUnavailable
from app.tools.base import ToolResult


class RunCommandArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    argv: list[str] = Field(min_length=1, max_length=256)
    cwd: str = Field(min_length=1, max_length=32_768)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, argv):
        if any(not item or "\x00" in item for item in argv):
            raise ValueError("argv entries must be non-empty and contain no NUL")
        if sum(len(item.encode("utf-8")) for item in argv) > 64 * 1024:
            raise ValueError("argv exceeds the request byte limit")
        return argv

    @field_validator("cwd")
    @classmethod
    def validate_cwd(cls, cwd):
        path = PurePosixPath(cwd)
        if "\\" in cwd or "\x00" in cwd or path.is_absolute() or ".." in path.parts:
            raise ValueError("cwd must stay within the workspace")
        return cwd


class RunCommand:
    name = "run_command"
    input_model = RunCommandArguments

    def __init__(self, backend: ExecutionBackend):
        self.backend = backend

    def definition(self):
        return ToolDefinition(
            name=self.name,
            description="Run a structured argv command in an isolated execution backend. Shell strings are not accepted.",
            parameters=self.input_model.model_json_schema(),
        )

    async def execute(self, arguments, context):
        self.input_model.model_validate(arguments)
        capabilities = await self.backend.describe_capabilities()
        code = ("execution_backend_orchestration_unavailable" if capabilities.command_execution
                else ExecutionBackendUnavailable.code)
        outcome = CapabilityOutcome(kind="failed", error=CapabilityError(
            category="unavailable", code=code))
        return ToolResult(success=False, data=outcome.model_dump(mode="json"))


def register_command_execution(registry, backend: ExecutionBackend):
    tool = RunCommand(backend)
    registry.register(tool, CapabilityDescriptor(
        name=tool.name,
        parameters=tool.definition().parameters,
        category="project",
        requires_workspace=True,
        side_effect="sandbox",
        recovery_class="sandbox",
        available=False,
        reason=ExecutionBackendUnavailable.code,
        constraints={"argv_only": True, "host_fallback": False},
    ))
