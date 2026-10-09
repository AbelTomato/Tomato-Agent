"""Persistence and completion boundary for controlled code tasks."""

from pathlib import Path
import asyncio
import base64
import hashlib
import inspect
import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

from app.agent.harness_models import Budget
from app.agent.harness import Harness
from app.agent.harness import Decision
from app.agent.models import CodeTaskRequest
from app.agent.models import LLMResponse
from app.agent.policies import CodeTaskCapabilityProfile
from app.errors import ToolTimeoutError
from app.artifacts.service import ArtifactService
from app.runs.repository import RunRepository
from app.tools.code_tests import RunTestsTool
from app.tools.code_workspace import create_code_workspace_registry
from app.tools.base import ToolContext
from app.tools.registry import ToolRegistry
from app.workspaces.service import WorkspaceService


class CodeTaskStrategy:
    """Translate provider function calls into controlled Harness decisions."""

    async def decide(self, state, available_tools, llm_response: LLMResponse | None = None):
        if llm_response is not None and llm_response.kind == "final":
            return Decision(kind="respond", output=llm_response.content or "")
        call = llm_response.tool_call if llm_response is not None else None
        if call is None:
            return Decision(kind="stop", reason="model did not request an allowed tool")
        return Decision(kind="call_tool", tool_name=call.name, arguments=call.arguments)


class CodeTaskService:
    def __init__(self, runs: RunRepository, workspaces: WorkspaceService,
                 artifacts: ArtifactService, *, max_test_retries: int = 1,
                 llm=None, strategy=None, test_tool: RunTestsTool | None = None,
                 budget: Budget | None = None,
                 capability_profile_factory: Callable[[Path], CodeTaskCapabilityProfile] | None = None):
        if max_test_retries < 0:
            raise ValueError("max_test_retries cannot be negative")
        self.run_repository = runs
        self.workspace_service = workspaces
        self.artifact_service = artifacts
        self.max_test_retries = max_test_retries
        self.test_results: dict[str, list[dict[str, Any]]] = {}
        self.diff_artifacts: dict[str, str] = {}
        self._executing: set[str] = set()
        self._execution_tasks: dict[str, asyncio.Task] = {}
        self._cancel_requested: set[str] = set()
        self._execution_errors: dict[str, str] = {}
        self.llm = llm
        self.strategy = strategy
        self.test_tool = test_tool
        self.budget = budget
        self.capability_profile_factory = capability_profile_factory

    def _build_harness(self, run, profile, test_tool):
        service = self

        class CodeTaskHarness(Harness):
            async def run(self, initial_state, strategy, *, llm, tools, policy, budget):
                registry = ToolRegistry()
                for declaration_tool in tools._tools.values():
                    if declaration_tool.name != "run_tests" or test_tool is None:
                        registry.register(declaration_tool)
                if test_tool is not None:
                    registry.register(test_tool)

                class TestFeedbackRegistry(ToolRegistry):
                    async def execute(self, name, arguments, context, timeout=20):
                        if name != "run_tests":
                            return await super().execute(name, arguments, context, timeout)
                        while True:
                            try:
                                validated = self.get(name).input_model.model_validate(arguments).model_dump()
                            except Exception:
                                service._execution_errors[str(run.id)] = "invalid_arguments"
                                await service._fail(run, "failed", "invalid_tool_arguments")
                                from app.tools.base import ToolResult
                                return ToolResult(success=False, error="invalid_tool_arguments")
                            try:
                                result = await super().execute(name, validated, context, timeout)
                            except asyncio.CancelledError:
                                service._execution_errors[str(run.id)] = "cancelled"
                                await service._fail(run, "cancelled", "cancelled")
                                raise
                            except (ToolTimeoutError, TimeoutError):
                                service._execution_errors[str(run.id)] = "timeout"
                                await service._fail(run, "timed_out", "timeout")
                                from app.tools.base import ToolResult
                                return ToolResult(success=False, error="timeout")
                            data = result.data if isinstance(result.data, dict) else {}
                            execution_status = data.get("status")
                            if execution_status in {"cancelled", "timed_out", "failed"}:
                                code = data.get("error_code") or execution_status
                                service._execution_errors[str(run.id)] = code
                                await service._fail(run, execution_status, code)
                            elif not result.success:
                                code = result.error or "execution_failed"
                                service._execution_errors[str(run.id)] = code
                                status = "timed_out" if code == "timeout" else "failed"
                                await service._fail(run, status, code)
                            elif isinstance(data.get("passed"), bool):
                                await service.record_test_result(
                                    str(run.id), target=data.get("target", "unknown"),
                                    passed=data["passed"], exit_code=data.get("exit_code"),
                                    artifact_id=(data.get("artifact") or {}).get("artifact_id"),
                                )
                                # A completed failing test is feedback for the next
                                # model turn. Every retest consumes Harness budget.
                            return result

                class RunBoundRegistry(TestFeedbackRegistry):
                    async def execute(self, name, arguments, context, timeout=20):
                        bound = context.model_copy(update={
                            "run_id": str(run.id), "workspace_id": run.workspace_id, "profile": profile,
                        })
                        result = await super().execute(name, arguments, bound, timeout)
                        if not result.success and name != "run_tests":
                            code = result.error or "execution_failed"
                            service._execution_errors[str(run.id)] = code
                            await service._fail(run, "failed", code)
                        return result

                bound_registry = RunBoundRegistry(list(registry._tools.values()))
                return await super(CodeTaskHarness, self).run(
                    initial_state, strategy, llm=llm, tools=bound_registry, policy=policy, budget=budget
                )

        return CodeTaskHarness()

    @classmethod
    def for_testing(cls, root: Path) -> "CodeTaskService":
        root = Path(root)
        workspaces = WorkspaceService(root / "workspaces")
        artifacts = ArtifactService(root / "artifacts", workspaces)
        return cls(RunRepository(root / "runs.db"), workspaces, artifacts)

    async def initialize(self) -> None:
        await self.run_repository.init()
        await self.artifact_service.init()

    async def create(self, request: CodeTaskRequest):
        await self.initialize()
        fixture = Path(__file__).resolve().parents[1] / "code_task_fixtures" / "python_calculator"
        workspace = await self.workspace_service.create()
        repo = workspace.repo_path
        for filename in ("calculator.py", "test_calculator.py"):
            (repo / filename).write_bytes((fixture / filename).read_bytes())
        metadata_path, metadata = self.workspace_service._metadata(workspace.workspace_id)
        metadata["baseline"] = {
            filename: base64.b64encode((repo / filename).read_bytes()).decode("ascii")
            for filename in ("calculator.py", "test_calculator.py")
        }
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        run = await self.run_repository.create_run("code_task", request.model_dump(), workspace.workspace_id)
        await self.run_repository.update_status(run.id, "running")
        await self.run_repository.append_event(run.id, "code_task.created", {"workspace_id": workspace.workspace_id})
        await self.run_repository.save_checkpoint(run.id, {"loop_count": 0, "tool_calls": 0, "test_attempts": 0})
        return await self.run_repository.get_run(run.id)

    def build_capability_profile(self, run) -> CodeTaskCapabilityProfile:
        if run is None:
            raise ValueError("run is required")
        repo = self.workspace_service._directory(run.workspace_id) / "repo"
        if self.capability_profile_factory is None:
            return CodeTaskCapabilityProfile(allowed_paths=(str(repo.resolve()),))
        return self.capability_profile_factory(repo)

    async def record_test_result(self, run_id: str, *, target: str, passed: bool,
                                 exit_code: int | None, artifact_id: str | None = None) -> None:
        run = await self.run_repository.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        workspace_diff = await self.workspace_service.diff(run.workspace_id)
        result = {"target": target, "passed": passed, "exit_code": exit_code,
                  "artifact_id": artifact_id,
                  "workspace_diff_sha256": hashlib.sha256(
                      workspace_diff.encode("utf-8")).hexdigest()}
        previous = await self.run_repository.get_checkpoint(run_id)
        state = dict(previous.state) if previous else {}
        test_results = list(state.get("test_results", []))
        test_results.append(result)
        self.test_results[str(run_id)] = test_results
        await self.run_repository.append_event(run_id, "code_task.test_result", result)
        state.update({"test_attempts": len(test_results), "test_results": test_results,
                      "last_test_result": result})
        await self.run_repository.save_checkpoint(run_id, state)

    async def run_test_with_retry(self, run_id: str, operation):
        run = await self.run_repository.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        last = None
        for attempt in range(self.max_test_retries + 1):
            try:
                last = operation()
                if inspect.isawaitable(last):
                    last = await last
            except asyncio.CancelledError:
                await self._fail(run, "cancelled", "cancelled")
                raise
            except TimeoutError:
                await self._fail(run, "timed_out", "timeout")
                return {"target": "unknown", "passed": False, "exit_code": None, "error_code": "timeout"}
            if not isinstance(last, dict) or not isinstance(last.get("passed"), bool):
                await self._fail(run, "failed", "invalid_tool_arguments")
                return {"target": "unknown", "passed": False, "exit_code": None,
                        "error_code": "invalid_tool_arguments"}
            await self.record_test_result(run_id, target=last.get("target", "unknown"),
                                          passed=last["passed"], exit_code=last.get("exit_code"),
                                          artifact_id=last.get("artifact_id"))
            if last["passed"] and last.get("exit_code") == 0:
                return last
            if attempt < self.max_test_retries:
                continue
        return last

    async def _fail(self, run, status: str, code: str) -> None:
        current = await self.run_repository.get_run(run.id)
        if current is None:
            raise KeyError(run.id)
        if current.status in {"completed", "failed", "cancelled", "timed_out"}:
            return
        await self.run_repository.update_status(run.id, status, error={"code": code})
        await self.run_repository.append_event(run.id, "code_task.failed", {"code": code})
        previous = await self.run_repository.get_checkpoint(run.id)
        state = dict(previous.state) if previous else {}
        state.update({"error_code": code, "status": status})
        await self.run_repository.save_checkpoint(run.id, state)

    async def record_diff(self, run_id: str) -> str:
        run = await self.run_repository.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        diff = await self.workspace_service.diff(run.workspace_id)
        artifact = await self.artifact_service.register_text(run.id, "diff", diff)
        self.diff_artifacts[str(run_id)] = str(artifact.artifact_id)
        previous = await self.run_repository.get_checkpoint(run_id)
        state = dict(previous.state) if previous else {}
        state["diff_artifact_id"] = str(artifact.artifact_id)
        await self.run_repository.save_checkpoint(run.id, state)
        return diff

    async def validate_completion(self, run_id: str, proposed_answer: str | None = None,
                                  allowed_paths: set[str] | None = None) -> bool:
        run = await self.run_repository.get_run(run_id)
        if run is None or run.status not in {"running", "waiting"}:
            return False
        checkpoint = await self.run_repository.get_checkpoint(run.id)
        state = dict(checkpoint.state) if checkpoint else {}
        tests = list(state.get("test_results", self.test_results.get(str(run.id), [])))
        if not tests or not tests[-1]["passed"] or tests[-1]["exit_code"] != 0:
            return False
        current_diff = await self.workspace_service.diff(run.workspace_id)
        current_diff_sha256 = hashlib.sha256(current_diff.encode("utf-8")).hexdigest()
        if tests[-1].get("workspace_diff_sha256") != current_diff_sha256:
            return False
        diff_id = state.get("diff_artifact_id") or self.diff_artifacts.get(str(run.id))
        if diff_id is None:
            return False
        try:
            diff_ref = await self.artifact_service.get(diff_id)
            diff = (await self.artifact_service.read(diff_ref.artifact_id, 1_000_000)).decode("utf-8")
            if diff != current_diff or not diff or not diff.startswith("diff --git "):
                return False
            changed = {line.split(" b/", 1)[1] for line in diff.splitlines()
                       if line.startswith("diff --git a/") and " b/" in line}
            if not changed or (allowed_paths is not None and not all(
                self._path_is_allowed(run.workspace_id, path, allowed_paths)
                for path in changed
            )):
                return False
            refs = await self.artifact_service.list_for_run(run.id)
            if not refs:
                return False
            for ref in refs:
                await self.artifact_service.read(ref.artifact_id, 1_000_000)
        except (KeyError, ValueError, OSError, UnicodeDecodeError):
            return False
        await self.run_repository.update_status(run.id, "completed")
        await self.run_repository.append_event(run.id, "code_task.completed", {
            "test_target": tests[-1]["target"], "diff_artifact_id": diff_id,
        })
        state.update({"test_results": tests, "diff_artifact_id": diff_id,
                      "completion_validated": True})
        await self.run_repository.save_checkpoint(run.id, state)
        return True

    def _path_is_allowed(self, workspace_id: str, relative_path: str,
                         allowed_paths: set[str]) -> bool:
        resolved = self.workspace_service.resolve(workspace_id, relative_path)
        for allowed in allowed_paths:
            allowed_path = Path(allowed)
            if allowed_path.is_absolute():
                if resolved.is_relative_to(allowed_path.resolve()):
                    return True
            elif relative_path == allowed_path.as_posix() or Path(relative_path).is_relative_to(allowed_path):
                return True
        return False

    async def cancel(self, run_id: str, reason: str = "cancelled"):
        run = await self.run_repository.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status not in {"completed", "failed", "cancelled", "timed_out"}:
            await self._fail(run, "cancelled", "cancelled")
        task = self._execution_tasks.get(str(run.id))
        if task is not None and not task.done() and task is not asyncio.current_task():
            if not task.cancelling():
                self._cancel_requested.add(str(run.id))
                task.cancel()
            # Wait for executor cleanup; cancellation of this HTTP request must
            # not send a second cancellation into the sandbox cleanup.
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
        return await self.run_repository.get_run(run.id)

    async def _result_details(self, run_id: str, *, usage=None, tool_calls: int = 0) -> dict[str, Any]:
        """Assemble the auditable result shared by success and failure responses."""
        run = await self.run_repository.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        events = await self.run_repository.list_events(run.id)
        artifacts = await self.artifact_service.list_for_run(run.id)
        checkpoint = await self.run_repository.get_checkpoint(run.id)
        state = dict(checkpoint.state) if checkpoint else {}
        persisted_usage = state.get("usage")
        persisted_tool_calls = state.get("tool_calls", 0)
        if usage is None and isinstance(persisted_usage, dict):
            usage_payload = persisted_usage
        else:
            usage_payload = usage.model_dump(mode="json") if usage is not None else {}
        if tool_calls == 0 and isinstance(persisted_tool_calls, int):
            tool_calls = persisted_tool_calls
        diff_artifact = None
        changed_files: list[str] = []
        diff_id = state.get("diff_artifact_id") or self.diff_artifacts.get(str(run.id))
        if diff_id is None:
            diff_refs = [ref for ref in artifacts if ref.kind == "diff"]
            if diff_refs:
                diff_id = str(diff_refs[-1].artifact_id)
        if diff_id is not None:
            diff_artifact = next((ref.model_dump(mode="json") for ref in artifacts
                                  if str(ref.artifact_id) == diff_id), None)
            if diff_artifact is not None:
                diff = (await self.artifact_service.read(diff_id, 1_000_000)).decode("utf-8")
                changed_files = [line.split(" b/", 1)[1] for line in diff.splitlines()
                                 if line.startswith("diff --git a/") and " b/" in line]
        return {
            "usage": usage_payload,
            "tool_calls": tool_calls,
            "events": [event.model_dump(mode="json") for event in events],
            "artifacts": [ref.model_dump(mode="json") for ref in artifacts],
            "test_results": state.get("test_results", self.test_results.get(str(run.id), [])),
            "diff_artifact": diff_artifact,
            "changed_files": changed_files,
        }

    async def execute(self, run_id: str, *, llm=None, strategy=None, test_tool: RunTestsTool | None = None,
                      budget: Budget | None = None):
        """Harness-driven execution is injected by the application composition root."""
        run = await self.run_repository.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        llm = llm or self.llm
        strategy = strategy or self.strategy
        test_tool = test_tool or self.test_tool
        budget = budget or self.budget
        if llm is None or strategy is None:
            raise ValueError("llm and strategy are required")
        if run.status != "running" or str(run.id) in self._executing:
            return {"status": "failed", "error": {"code": "run_not_active"},
                    **await self._result_details(run_id)}
        self._executing.add(str(run.id))
        from app.agent.harness import Harness
        from app.agent.harness_models import HarnessState, ToolPolicy
        from app.agent.models import ContextState
        from app.tools.registry import ToolRegistry

        registry = create_code_workspace_registry(self.workspace_service, self.artifact_service,
                                                  self.run_repository)
        if test_tool is not None:
            registry.register(test_tool)
        profile = self.build_capability_profile(run)
        active_budget = budget or Budget(
            max_loops=12, max_tool_calls=8, max_duration_seconds=120,
            max_context_tokens=8000, max_response_chars=20000)
        policy = ToolPolicy(allowed_tools=profile.allowed_tools, max_calls=active_budget.max_tool_calls,
            timeout_seconds=profile.timeout_seconds, max_result_chars=profile.max_output_chars)
        state = HarnessState(task_id=str(run.id), structured_state={"task": run.request.get("task"),
            "workspace_id": run.workspace_id}, context_state=ContextState())
        try:
            execution = asyncio.create_task(self._build_harness(run, profile, test_tool).run(
                state, strategy, llm=llm, tools=registry, policy=policy,
                budget=active_budget,
            ))
            self._execution_tasks[str(run.id)] = execution
            result = await execution
        except asyncio.CancelledError:
            current = await self.run_repository.get_run(run.id)
            if str(run.id) in self._cancel_requested and current is not None and current.status == "cancelled":
                return {"status": "failed", "error": {"code": "cancelled"},
                        **await self._result_details(run_id)}
            await self._fail(run, "cancelled", "cancelled")
            raise
        finally:
            self._execution_tasks.pop(str(run.id), None)
            self._cancel_requested.discard(str(run.id))
            self._executing.discard(str(run.id))
        if str(run.id) not in self.diff_artifacts:
            diff_refs = [ref for ref in await self.artifact_service.list_for_run(run.id)
                         if ref.kind == "diff"]
            if diff_refs:
                self.diff_artifacts[str(run.id)] = str(diff_refs[-1].artifact_id)
        checkpoint = await self.run_repository.get_checkpoint(run.id)
        state = dict(checkpoint.state) if checkpoint else {}
        state.update({"harness_status": result.status, "tool_calls": result.tool_calls,
                      "usage": result.usage.model_dump(mode="json"),
                      "output": str(result.output)[:20000]})
        await self.run_repository.save_checkpoint(run.id, state)
        details = await self._result_details(run_id, usage=result.usage, tool_calls=result.tool_calls)
        if result.status == "completed" and await self.validate_completion(
                str(run.id), str(result.output), allowed_paths=set(profile.allowed_paths)):
            details = await self._result_details(run_id, usage=result.usage, tool_calls=result.tool_calls)
            return {"status": "completed", "answer": result.output, **details}
        code = self._execution_errors.pop(str(run.id), None)
        if result.status == "stopped" and code == "tests_failed":
            code = "budget_exhausted"
        if code is None and result.stop_reason == "invalid tool call":
            code = "invalid_arguments"
            await self._fail(run, "failed", code)
        if code is None:
            tests = state.get("test_results", [])
            if result.status == "stopped":
                code = "budget_exhausted"
            elif tests and (not tests[-1]["passed"] or tests[-1]["exit_code"] != 0):
                code = "tests_failed"
            else:
                code = "completion_validation_failed"
        current = await self.run_repository.get_run(run.id)
        if current and current.status in {"running", "waiting"}:
            await self._fail(run, "failed", code)
        details = await self._result_details(run_id, usage=result.usage, tool_calls=result.tool_calls)
        return {"status": "failed", "error": {"code": code}, **details}