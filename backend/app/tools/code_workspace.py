"""Opt-in workspace tools. All authority comes from server-created context."""

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.models import ToolDefinition
from app.agent.policies import CodeTaskCapabilityProfile, ToolDeclaration
from app.artifacts.service import ArtifactService
from app.runs.repository import RunRepository
from app.workspaces.service import WorkspacePathError, WorkspaceService

from .base import ToolContext, ToolResult
from .registry import ToolRegistry


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DirectoryArguments(Arguments):
    path: str = ""


class FileArguments(Arguments):
    path: str = Field(min_length=1)


class WriteArguments(FileArguments):
    content: str


class SearchArguments(DirectoryArguments):
    query: str = Field(min_length=1)


class PatchChange(FileArguments):
    old_text: str = Field(min_length=1)
    new_text: str


class PatchArguments(Arguments):
    changes: list[PatchChange] = Field(min_length=1, max_length=100)


class CollectArguments(FileArguments):
    kind: str = Field(default="file", min_length=1, max_length=100)


class CodeWorkspaceTool:
    def __init__(self, name: str, input_model: type[BaseModel],
                 workspaces: WorkspaceService, artifacts: ArtifactService,
                 runs: RunRepository, *, persist_events: bool = True):
        self.name = name
        self.description = f"{name} within the current authorized code workspace."
        self.input_model = input_model
        self.workspaces = workspaces
        self.artifacts = artifacts
        self.runs = runs
        self.persist_events = persist_events

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description=self.description,
                              parameters=self.input_model.model_json_schema())

    def declaration(self) -> ToolDeclaration:
        return ToolDeclaration(
            name=self.name, description=self.description,
            input_schema=self.input_model.model_json_schema(),
            capabilities=frozenset({"file", "resource"}),
            side_effect="write" if self.name in {
                "write_file", "apply_patch", "collect_artifact", "get_diff"
            } else "read",
        )

    def _authorize_path(self, context: ToolContext, path: str, write: bool = False):
        resolved = self.workspaces.resolve(context.workspace_id, path, write=write)
        if not any(resolved.is_relative_to(Path(root).resolve())
                   for root in context.profile.allowed_paths):
            raise PermissionError
        return resolved

    @staticmethod
    def _bounded(data: dict, limit: int) -> ToolResult:
        """Keep typed fields and artifact references; trim only preview fields."""
        data["truncated"] = False
        def size():
            return len(json.dumps(data, ensure_ascii=False))
        while size() > limit:
            data["truncated"] = True
            key = next((key for key in ("content", "diff", "matches", "files")
                        if data.get(key)), None)
            if key is None:
                return ToolResult(success=False, error="output_limit")
            value = data[key]
            if isinstance(value, str):
                data[key] = value[:max(0, len(value) - max(1, size() - limit))]
            else:
                data[key] = value[:-1]
        return ToolResult(success=True, data=data)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            args = self.input_model.model_validate(arguments)
            if not isinstance(context.profile, CodeTaskCapabilityProfile):
                raise PermissionError
            # Revalidate even if an internal caller used model_copy(update=...).
            profile = CodeTaskCapabilityProfile.model_validate(context.profile.model_dump())
            if not context.workspace_id or self.name not in profile.allowed_tools:
                raise PermissionError
            run = await self.runs.get_run(context.run_id)
            if run is None or run.workspace_id != context.workspace_id:
                raise PermissionError
            self._authorize_path(context, "")
            data = await self._execute(args, context)
            return self._bounded(data, profile.max_output_chars)
        except PermissionError:
            return ToolResult(success=False, error="permission_denied")
        except WorkspacePathError:
            return ToolResult(success=False, error="permission_denied")
        except (ValidationError, ValueError, TypeError):
            return ToolResult(success=False, error="invalid_arguments")
        except (KeyError, OSError):
            return ToolResult(success=False, error="execution_failed")

    async def _execute(self, args: BaseModel, context: ToolContext) -> dict:
        workspace_id = context.workspace_id
        if self.name in {"list_files", "search_files", "read_file", "write_file", "collect_artifact"}:
            self._authorize_path(context, args.path, write=self.name == "write_file")
        if self.name == "list_files":
            files = await self.workspaces.list_files(workspace_id, args.path)
            for path in files:
                self._authorize_path(context, path)
            return {"files": files}
        if self.name == "read_file":
            return {"content": await self.workspaces.read_file(
                workspace_id, args.path, self.workspaces.max_file_bytes)}
        if self.name == "write_file":
            await self.workspaces.write_file(workspace_id, args.path, args.content)
            return {"path": args.path}
        if self.name == "search_files":
            matches = []
            for path in await self.workspaces.list_files(workspace_id, args.path):
                self._authorize_path(context, path)
                content = await self.workspaces.read_file(workspace_id, path, self.workspaces.max_file_bytes)
                for number, line in enumerate(content.splitlines(), 1):
                    if args.query in line:
                        matches.append({"path": path, "line": number, "text": line})
            return {"matches": matches}
        if self.name == "apply_patch":
            pending = {}
            for change in args.changes:
                path = self._authorize_path(context, change.path, write=True)
                if path in pending:
                    raise ValueError("Duplicate patch path")
                content = await self.workspaces.read_file(
                    workspace_id, change.path, self.workspaces.max_file_bytes)
                if content.count(change.old_text) != 1:
                    raise ValueError("Patch must match exactly once")
                updated = content.replace(change.old_text, change.new_text, 1)
                if len(updated.encode("utf-8")) > self.workspaces.max_file_bytes:
                    raise ValueError("File too large")
                pending[path] = (change.path, updated)
            for relative_path, content in pending.values():
                await self.workspaces.write_file(workspace_id, relative_path, content)
            return {"files": [relative for relative, _ in pending.values()]}
        if self.name == "get_diff":
            diff = await self.workspaces.diff(workspace_id)
            ref = await self.artifacts.register_text(context.run_id, "diff", diff)
            await self.workspaces.record_artifact_ref(workspace_id, ref.model_dump(mode="json"))
            if self.persist_events:
                await self.runs.append_event(context.run_id, "code_task.diff_created", {
                    "artifact_id": str(ref.artifact_id),
                })
            return {"diff": diff, "artifact": ref.model_dump(mode="json")}
        if self.name == "collect_artifact":
            ref = await self.artifacts.register_file(context.run_id, workspace_id, args.path, args.kind)
            return {"artifact": ref.model_dump(mode="json")}
        raise ValueError("Unknown code tool")


def create_code_workspace_registry(workspaces: WorkspaceService, artifacts: ArtifactService,
                                   runs: RunRepository, *, persist_events: bool = True) -> ToolRegistry:
    """Explicit factory; ordinary Session registries never receive these tools."""
    models = {
        "list_files": DirectoryArguments, "read_file": FileArguments,
        "search_files": SearchArguments, "write_file": WriteArguments,
        "apply_patch": PatchArguments, "get_diff": Arguments,
        "collect_artifact": CollectArguments,
    }
    return ToolRegistry([CodeWorkspaceTool(name, model, workspaces, artifacts, runs,
                                           persist_events=persist_events)
                         for name, model in models.items()])