"""Persistence and completion boundary for controlled code tasks."""

from pathlib import Path
import asyncio
import base64
import hashlib
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
from app.artifacts.service import ArtifactService
from app.runs.repository import RunRepository
from app.tools.code_tests import RunTestsTool
from app.tools.registry import ToolRegistry
from app.workspaces.service import WorkspaceService
from app.execution.repository import ExecutionRepository
from app.execution.recovery import RecoveryConflict, RecoveryObservation


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
                 artifacts: ArtifactService, *,
                 llm=None, strategy=None, test_tool: RunTestsTool | None = None,
                 budget: Budget | None = None,
                 capability_profile_factory: Callable[[Path], CodeTaskCapabilityProfile] | None = None,
                 execution_repository: ExecutionRepository | None = None):
        self.run_repository = runs
        self.workspace_service = workspaces
        self.artifact_service = artifacts
        self.diff_artifacts: dict[str, str] = {}
        self._execution_tasks: dict[str, asyncio.Task] = {}
        self._cancel_requested: set[str] = set()
        self.llm = llm
        self.strategy = strategy
        self.test_tool = test_tool
        self.budget = budget
        self.capability_profile_factory = capability_profile_factory
        self.execution_repository = execution_repository or ExecutionRepository(runs.path)
        self.heartbeat_seconds = 10
        self.lease_seconds = 30
        self._heartbeat_tasks: dict[str, asyncio.Task] = {}

    def _build_harness(self, run, profile, test_tool):
        service = self

        class CodeTaskHarness(Harness):
            async def run(self, initial_state, strategy, *, llm, tools, policy, budget,
                          execution_port=None, resume_snapshot=None):
                registry = ToolRegistry()
                for declaration_tool in tools._tools.values():
                    if declaration_tool.name != "run_tests" or test_tool is None:
                        registry.register(declaration_tool)
                if test_tool is not None:
                    registry.register(test_tool)

                class TestFeedbackRegistry(ToolRegistry):
                    async def execute(self, name, arguments, context, timeout=20):
                        return await super().execute(name, arguments, context, timeout)

                class RunBoundRegistry(TestFeedbackRegistry):
                    async def execute(self, name, arguments, context, timeout=20):
                        bound = context.model_copy(update={
                            "run_id": str(run.id), "workspace_id": run.workspace_id, "profile": profile,
                        })
                        result = await super().execute(name, arguments, bound, timeout)
                        return result

                bound_registry = RunBoundRegistry(list(registry._tools.values()))
                return await super(CodeTaskHarness, self).run(
                    initial_state, strategy, llm=llm, tools=bound_registry, policy=policy, budget=budget,
                    execution_port=execution_port, resume_snapshot=resume_snapshot,
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
        if self.execution_repository is not None:
            await self.execution_repository.init()

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
        from app.agent.code_execution import initial_snapshot
        await self.execution_repository.bootstrap_run(
            initial_snapshot(self, run),
            event_type="code_task.created",
            event_payload={"workspace_id": workspace.workspace_id},
        )
        return await self.run_repository.get_run(run.id)

    async def recover(self, run_id: str, *, expected_version: int, action: str):
        run = await self.run_repository.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.version != expected_version:
            raise RecoveryConflict("version_conflict")
        try:
            candidate = await self.execution_repository.get_recovery_candidate(run.id)
        except RecoveryConflict as exc:
            if str(exc) == "legacy_execution":
                raise RecoveryConflict("legacy_snapshot") from exc
            raise
        observation = RecoveryObservation(policy_digest=(candidate.snapshot or {}).get("policy_digest", ""),
                                          executor_exited=False)
        from app.execution.recovery import classify_recovery
        decision = classify_recovery(candidate.snapshot, candidate.operation, observation)
        if action == "inspect":
            return {"status": run.status, "recovery_reason": decision.reason_code,
                    "recovery_action": decision.action}
        if action != "continue":
            raise RecoveryConflict("invalid_recovery_action")
        attempt = await self.execution_repository.get_attempt(candidate.attempt_id)
        if attempt.status == "running":
            candidate = await self.execution_repository.expire_and_classify(attempt.id)
            decision = classify_recovery(candidate.snapshot, candidate.operation, observation)
        recovered = await self.execution_repository.apply_recovery(candidate, decision)
        if recovered.status == "waiting":
            return {"status": "waiting", "recovery_reason": decision.reason_code}
        return await self.execute(run_id)

    def build_capability_profile(self, run) -> CodeTaskCapabilityProfile:
        if run is None:
            raise ValueError("run is required")
        repo = self.workspace_service._directory(run.workspace_id) / "repo"
        if self.capability_profile_factory is None:
            return CodeTaskCapabilityProfile(allowed_paths=(str(repo.resolve()),))
        return self.capability_profile_factory(repo)

    async def validate_completion(self, run_id: str, proposed_answer: str | None = None,
                                  allowed_paths: set[str] | None = None) -> bool:
        run = await self.run_repository.get_run(run_id)
        if run is None or run.status not in {"running", "waiting"}:
            return False
        checkpoint = await self.run_repository.get_checkpoint(run.id)
        state = dict(checkpoint.state) if checkpoint else {}
        if state.get("protocol_version") == 1:
            state = {**state, **state.get("harness_state", {})}
        tests = list(state.get("test_results", []))
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
            if self.execution_repository is not None:
                await self.execution_repository.request_cancel(run.id)
            else:
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
        if state.get("protocol_version") == 1:
            state = {**state, **state.get("harness_state", {})}
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
            "test_results": state.get("test_results", []),
            "diff_artifact": diff_artifact,
            "changed_files": changed_files,
        }

    async def execute(self, run_id: str, *, llm=None, strategy=None,
                      test_tool: RunTestsTool | None = None, budget: Budget | None = None,
                      execution_lease=None):
        run = await self.run_repository.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        llm = llm or self.llm
        strategy = strategy or self.strategy
        test_tool = test_tool or self.test_tool
        budget = budget or self.budget
        if llm is None or strategy is None:
            if execution_lease is None:
                await self.execution_repository.fail_unclaimed(run.id, "model_not_configured")
            else:
                await self.execution_repository.finish(execution_lease, "failed",
                    event_type="code_task.failed", error={"code": "model_not_configured"})
            return {"status": "failed", "error": {"code": "model_not_configured"},
                    **await self._result_details(run_id)}
        from app.agent.code_execution import execute_code
        return await execute_code(self, run, llm=llm, strategy=strategy,
                                  test_tool=test_tool, budget=budget, execution_lease=execution_lease)