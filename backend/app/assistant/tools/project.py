import json

from pydantic import BaseModel, ConfigDict, Field

from app.agent.models import ToolDefinition
from app.assistant.models import AssistantResult, CapabilityDescriptor, CapabilityError, CapabilityOutcome
from app.tools.base import ToolResult
from app.tools.code_workspace import CodeWorkspaceTool, DirectoryArguments, FileArguments, SearchArguments


class ClarificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class RequestInput:
    name = "request_input"
    input_model = ClarificationInput

    def definition(self):
        return ToolDefinition(name=self.name, description="Ask the user for missing information, then wait.",
                              parameters=self.input_model.model_json_schema())


def register_read_only(registry, tool, *, workspace=False):
    registry.register(tool, CapabilityDescriptor(name=tool.name, parameters=tool.definition().parameters,
        requires_workspace=workspace, side_effect="read_only", recovery_class="read_only", available=True))


class ProjectRead:
    def __init__(self, tool):
        self.tool = tool
        self.name = tool.name
        self.input_model = tool.input_model

    def definition(self):
        return self.tool.definition()

    async def execute(self, arguments, context):
        result = await self.tool.execute(arguments, context)
        if result.success:
            outcome = CapabilityOutcome(kind="result", result=AssistantResult(
                answer=json.dumps(result.data, ensure_ascii=False)))
        else:
            outcome = CapabilityOutcome(kind="failed", error=CapabilityError(
                category="permission_denied" if result.error == "permission_denied" else "recoverable_error",
                code=result.error or "project_read_failed"))
        return ToolResult(success=result.success, data=outcome.model_dump(mode="json"))


def register_project_reads(registry, workspaces, artifacts, runs):
    for name, model in (("list_files", DirectoryArguments), ("read_file", FileArguments), ("search_files", SearchArguments)):
        register_read_only(registry, ProjectRead(CodeWorkspaceTool(name, model, workspaces, artifacts, runs,
                          persist_events=False)), workspace=True)
    register_read_only(registry, RequestInput())