"""Assistant contract for server-controlled, read-only Docker tests."""
import json

from pydantic import BaseModel, ConfigDict, Field

from app.agent.models import ToolDefinition
from app.agent.policies import CodeTaskCapabilityProfile
from app.agent.sandbox_docker import DockerSandboxExecutor
from app.assistant.models import AssistantResult, CapabilityDescriptor, CapabilityError, CapabilityOutcome
from app.tools.base import ToolResult
from app.tools.code_tests import RunTestsTool, TestTargetRegistry
from app.runs.models import PendingAction
from app.artifacts.models import ArtifactRef


class TestArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target: str = Field(default="unit", pattern="^unit$")


class AssistantTests:
    name = "run_tests"
    input_model = TestArguments

    def __init__(self, workspaces, artifacts, runs, execution, config):
        self.workspaces, self.artifacts, self.runs = workspaces, artifacts, runs
        self.execution, self.config = execution, config

    def definition(self):
        return ToolDefinition(name=self.name, description="Run the registered unit tests in read-only Docker.",
                              parameters=self.input_model.model_json_schema())

    async def preflight(self, arguments, context):
        if not isinstance(context.profile, CodeTaskCapabilityProfile):
            raise PermissionError("permission_denied")
        if (context.profile.sandbox_backend != "docker" or self.name not in context.profile.allowed_tools
                or not context.workspace_id):
            raise PermissionError("permission_denied")
        run = await self.runs.get_run(context.run_id)
        if (run is None or run.workspace_id != context.workspace_id
                or run.request.get("options", {}).get("allow_test_execution") is not True):
            raise PermissionError("permission_denied")
        workspace = await self.workspaces.get(context.workspace_id)
        root = self.workspaces.root_path(context.workspace_id)
        if workspace.mode != "original" or str(root) not in context.profile.allowed_paths:
            raise PermissionError("permission_denied")
        self.input_model.model_validate(arguments)
        TestTargetRegistry.for_workspace(self.workspaces, context.workspace_id)

    async def execute_with_timeout(self, arguments, context, timeout):
        return await self.execute(arguments, context, timeout=timeout)

    async def execute(self, arguments, context, *, timeout=None):
        await self.preflight(arguments, context)
        targets = TestTargetRegistry.for_workspace(self.workspaces, context.workspace_id)
        target = targets.resolve(arguments.get("target", "unit"))
        executor = DockerSandboxExecutor(self.config.code_task_docker_image,
            docker_binary=self.config.code_task_docker_binary,
            docker_host="unix:///var/run/docker.sock",
            registered_targets={str(self.workspaces.root_path(context.workspace_id)): {target.name: target.command}})
        from app.execution.docker import TrackedDockerExecutor
        executor = TrackedDockerExecutor(executor, self.execution, context)
        tool = RunTestsTool(targets, executor, self.workspaces, self.artifacts, self.runs)
        raw = await tool.execute({"target": target.name}, context, timeout=timeout)
        if raw.data and raw.data.get("status") == "completed":
            report = dict(raw.data)
            report.update(invocation_id=context.invocation_id, step_id=context.step_id)
            artifact = report.get("artifact")
            outcome = CapabilityOutcome(kind="result", result=AssistantResult(
                answer=json.dumps(report, ensure_ascii=False), test_results=[report],
                artifacts=[ArtifactRef.model_validate_json(json.dumps(artifact))] if artifact else [],
                validation_status="passed" if report["passed"] else "failed"))
        else:
            code = raw.error or "test_execution_failed"
            unknown = code in {"cleanup_failed", "termination_unknown", "timeout", "cancelled", "execution_failed"}
            outcome = CapabilityOutcome(kind="waiting" if unknown else "failed",
                result=AssistantResult(test_results=[{**(raw.data or {}), "invocation_id": context.invocation_id,
                    "step_id": context.step_id}], validation_status="unavailable"),
                pending_action=PendingAction(action_id=f"recovery-{context.invocation_id}",
                    reason="unknown_side_effect", business_version=0, action_type="recovery",
                    allowed_actions=["cancel", "inspect_recovery"]) if unknown else None,
                error=CapabilityError(category="unknown_side_effect" if unknown else "recoverable_error", code=code))
        return ToolResult(success=outcome.kind == "result", data=outcome.model_dump(mode="json"))


def register_tests(registry, workspaces, artifacts, runs, execution, config):
    tool = AssistantTests(workspaces, artifacts, runs, execution, config)
    available = config.code_task_sandbox_backend == "docker" and bool(config.code_task_docker_image)
    registry.register(tool, CapabilityDescriptor(name=tool.name, parameters=tool.definition().parameters,
        category="project", requires_workspace=True, side_effect="sandbox", recovery_class="sandbox",
        available=available, reason=None if available else "docker_not_configured",
        constraints={"executor": "docker", "read_only_workspace": True, "requires_execution_authorization": True}))