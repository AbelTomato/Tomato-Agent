import json
import hashlib
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.agent.models import ToolDefinition
from app.assistant.models import AssistantResult, CapabilityDescriptor, CapabilityError, CapabilityOutcome
from app.tools.base import ToolResult
from app.tools.code_workspace import CodeWorkspaceTool, DirectoryArguments, FileArguments, SearchArguments
from app.agent.policies import CodeTaskCapabilityProfile
from app.workspaces.service import WorkspacePathError
from app.desktop.capability_bridge import DesktopBridgeError
from app.desktop.changesets import ChangeSet, ProposedFileChange
from app.runs.models import PendingAction


class ProjectReadAuthorizationRequired(PermissionError):
    pass


class ClarificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class RequestInput:
    name = "request_input"
    input_model = ClarificationInput

    def definition(self):
        return ToolDefinition(name=self.name, description="Ask the user for missing information, then wait.",
                              parameters=self.input_model.model_json_schema())


class ProposeChangeSetArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    changes: list[ProposedFileChange] = Field(min_length=1, max_length=100)


class ProposeChangeSet:
    name = "propose_changeset"
    input_model = ProposeChangeSetArguments

    def __init__(self, runs, sessions):
        self.runs = runs
        self.sessions = sessions
        self.bridge = None

    def definition(self):
        return ToolDefinition(
            name=self.name,
            description="Propose project file additions or modifications for explicit review and approval.",
            parameters=self.input_model.model_json_schema(),
        )

    async def preflight(self, arguments, context):
        run = await self.runs.get_run(context.run_id)
        session = await self.sessions.get_session(context.session_id)
        if (run is None or str(run.session_id) != context.session_id or
                session is None or not context.project_id or
                session.project_id != context.project_id):
            raise PermissionError("permission_denied")
        if not context.project_read_grant:
            raise ProjectReadAuthorizationRequired("project_read_authorization_required")

    async def execute(self, arguments, context):
        grant_version = hashlib.sha256(context.project_read_grant.encode("utf-8")).hexdigest()
        changeset = ChangeSet(
            change_id=str(uuid4()),
            run_id=context.run_id,
            session_id=context.session_id,
            project_id=context.project_id,
            authorization_version=grant_version,
            changes=arguments["changes"],
        )
        action = PendingAction(
            action_id=str(uuid4()),
            reason="changeset_approval_required",
            business_version=0,
            action_type="changeset_approval",
            target_capability=self.name,
            frozen_parameters={"change_id": changeset.change_id, "digest": changeset.digest},
            display_data={"changeset": changeset.model_dump(mode="json"), "digest": changeset.digest},
            allowed_actions=["approve_action", "reject_action"],
        )
        outcome = CapabilityOutcome(kind="waiting", pending_action=action)
        return ToolResult(success=True, data=outcome.model_dump(mode="json"))


def register_read_only(registry, tool, *, workspace=False, category="general"):
    registry.register(tool, CapabilityDescriptor(name=tool.name, parameters=tool.definition().parameters,
        requires_workspace=workspace, side_effect="read_only", recovery_class="read_only", available=True,
        category=category))


class ProjectRead:
    def __init__(self, tool, sessions=None):
        self.tool = tool
        self.name = tool.name
        self.input_model = tool.input_model
        self.sessions = sessions
        self.bridge = None

    def definition(self):
        return self.tool.definition()

    async def preflight(self, arguments, context):
        try:
            run = await self.tool.runs.get_run(context.run_id)
            if run is None or str(run.session_id) != context.session_id:
                raise PermissionError
            if self.bridge is not None:
                if self.name not in {"list_files", "search_files", "read_file"}:
                    raise PermissionError
                session = await self.sessions.get_session(context.session_id)
                if (not context.project_id or session is None or session.project_id != context.project_id):
                    raise PermissionError
                if not context.project_read_grant:
                    raise ProjectReadAuthorizationRequired
                return
            if not isinstance(context.profile, CodeTaskCapabilityProfile):
                raise PermissionError
            profile = CodeTaskCapabilityProfile.model_validate(context.profile.model_dump())
            if self.name not in profile.allowed_tools:
                raise PermissionError
            if not context.workspace_id or run.workspace_id != context.workspace_id:
                raise PermissionError
            self.tool._authorize_path(context, "")
            self.tool._authorize_path(context, arguments.get("path", ""))
        except ProjectReadAuthorizationRequired:
            raise
        except (PermissionError, WorkspacePathError, KeyError):
            raise PermissionError("permission_denied") from None

    async def execute(self, arguments, context):
        if self.bridge is not None:
            try:
                if not context.project_id or not context.project_read_grant:
                    raise PermissionError
                if self.name == "list_files":
                    result = await self.bridge.call("list_entries", {"relative_path": arguments.get("path", "") or "."},
                        grant=context.project_read_grant, session_id=context.session_id,
                        run_id=context.run_id, project_id=context.project_id)
                    data = {"files": result.get("files", []), "truncated": result.get("truncated", False)}
                elif self.name == "search_files":
                    result = await self.bridge.call("search_text", {
                        "relative_path": arguments.get("path", "") or ".",
                        "query": arguments["query"], "max_results": 100,
                    }, grant=context.project_read_grant, session_id=context.session_id,
                        run_id=context.run_id, project_id=context.project_id)
                    data = {"matches": result.get("matches", []), "truncated": result.get("truncated", False)}
                else:
                    result = await self.bridge.call("read_file", {
                        "relative_path": arguments["path"], "max_bytes": 131072,
                    }, grant=context.project_read_grant, session_id=context.session_id,
                        run_id=context.run_id, project_id=context.project_id)
                    data = {"content": result.get("content", ""), "missing": False}
                outcome = CapabilityOutcome(kind="result", result=AssistantResult(
                    answer=json.dumps(data, ensure_ascii=False)))
                return ToolResult(success=True, data=outcome.model_dump(mode="json"))
            except ProjectReadAuthorizationRequired:
                raise
            except DesktopBridgeError as error:
                outcome = CapabilityOutcome(kind="failed", error=CapabilityError(
                    category="permission_denied" if error.code.startswith("grant_") else "recoverable_error",
                    code=error.code))
                return ToolResult(success=False, data=outcome.model_dump(mode="json"))
        result = await self.tool.execute(arguments, context)
        if result.success:
            outcome = CapabilityOutcome(kind="result", result=AssistantResult(
                answer=json.dumps(result.data, ensure_ascii=False)))
        else:
            outcome = CapabilityOutcome(kind="failed", error=CapabilityError(
                category="permission_denied" if result.error == "permission_denied" else "recoverable_error",
                code=result.error or "project_read_failed"))
        return ToolResult(success=result.success, data=outcome.model_dump(mode="json"))


def register_project_reads(registry, workspaces, artifacts, runs, sessions=None):
    for name, model in (("list_files", DirectoryArguments), ("read_file", FileArguments), ("search_files", SearchArguments)):
        register_read_only(registry, ProjectRead(CodeWorkspaceTool(name, model, workspaces, artifacts, runs,
                          persist_events=False), sessions), workspace=True, category="project")
    register_read_only(registry, RequestInput())
    if sessions is not None:
        tool = ProposeChangeSet(runs, sessions)
        registry.register(tool, CapabilityDescriptor(name=tool.name, parameters=tool.definition().parameters,
            requires_workspace=False, side_effect="read_only", recovery_class="read_only", available=True,
            category="project"))
