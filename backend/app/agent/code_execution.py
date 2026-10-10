"""Controlled code-task adapter for the lease-bound execution kernel."""

import asyncio
import hashlib
from uuid import uuid4

from app.agent.harness import Harness
from app.agent.harness_models import Budget, HarnessState, ToolPolicy
from app.agent.models import ContextState
from app.execution.models import ExecutionSnapshot, OperationInput
from app.execution.ports import LeaseExecutionPort, canonical_digest
from app.execution.recovery import RecoveryConflict
from app.execution.state_machine import StaleExecutionOwner
from app.tools.code_workspace import create_code_workspace_registry


def execution_config(service, run, budget=None):
    profile = service.build_capability_profile(run)
    budget = budget or service.budget or Budget(max_loops=12, max_tool_calls=8,
        max_duration_seconds=120, max_context_tokens=8000, max_response_chars=20000)
    policy = ToolPolicy(allowed_tools=profile.allowed_tools, max_calls=budget.max_tool_calls,
        timeout_seconds=profile.timeout_seconds, max_result_chars=profile.max_output_chars)
    return profile, budget, policy


def initial_snapshot(service, run):
    _, budget, policy = execution_config(service, run)
    state = HarnessState(task_id=str(run.id), structured_state={"task": run.request["task"],
        "workspace_id": run.workspace_id}, context_state=ContextState())
    from app.agent.models import Message
    return ExecutionSnapshot(run_id=run.id, schema_version=1, checkpoint_revision=1,
        last_event_sequence=1, phase="ready_model", logical_index=0,
        messages=[Message(role="user", content=run.request["task"])],
        harness_state=state.model_dump(mode="json"), budget_limits=budget.model_dump(mode="json"),
        reserved_model_calls=0, reserved_tool_calls=0, loop_count=0,
        elapsed_upper_bound_seconds=0.0, policy_digest=canonical_digest(policy.model_dump(mode="json")),
        workspace_id=run.workspace_id, test_results=[], artifact_ids=[])


class CodeExecutionPort(LeaseExecutionPort):
    def __init__(self, service, lease, workspace_id):
        super().__init__(service.execution_repository, lease, workspace_id)
        self.service = service

    async def describe_operation(self, logical_index, tool_name, arguments):
        recovery_class = "file_write"
        before, after = {}, {}
        if tool_name in {"read_file", "list_files", "search_files"}:
            recovery_class = "read_only"
        elif tool_name == "run_tests":
            recovery_class = "sandbox"
        elif tool_name in {"get_diff", "collect_artifact"}:
            recovery_class = "artifact"
        elif tool_name in {"write_file", "apply_patch"}:
            changes = arguments.get("changes", []) if tool_name == "apply_patch" else [arguments]
            for change in changes:
                path = self.service.workspace_service.resolve(self.workspace_id, change["path"], write=True)
                content = path.read_bytes() if path.exists() else None
                before[change["path"]] = hashlib.sha256(content).hexdigest() if content is not None else "missing"
                if tool_name == "write_file":
                    updated = change["content"]
                else:
                    text = content.decode("utf-8")
                    if text.count(change["old_text"]) != 1:
                        raise ValueError("Patch must match exactly once")
                    updated = text.replace(change["old_text"], change["new_text"], 1)
                after[change["path"]] = hashlib.sha256(updated.encode("utf-8")).hexdigest()
        return OperationInput(id=uuid4(), logical_index=logical_index, kind="tool", tool_name=tool_name,
            input_payload=arguments, recovery_class=recovery_class, before_digests=before, after_digests=after)

    async def commit_step(self, step_id, result, snapshot):
        operation = await self.get_operation(snapshot.pending_operation_id) if snapshot.pending_operation_id else None
        data = result.result_payload.get("data") or {}
        tests = list(snapshot.test_results)
        artifacts = list(snapshot.artifact_ids)
        state = dict(snapshot.harness_state)
        if isinstance(data, dict):
            state.update({key: value for key, value in data.items()
                          if key in {"status", "error_code"}})
            ref = data.get("artifact") or {}
            if ref.get("artifact_id") and ref["artifact_id"] not in artifacts:
                artifacts.append(ref["artifact_id"])
            if isinstance(data.get("passed"), bool):
                diff = await self.service.workspace_service.diff(self.workspace_id)
                tests.append({"target": data.get("target", "unknown"), "passed": data["passed"],
                    "exit_code": data.get("exit_code"), "artifact_id": ref.get("artifact_id"),
                    "workspace_diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest()})
            if ref.get("kind") == "diff":
                state["diff_artifact_id"] = ref["artifact_id"]
            if not result.success:
                state["error_code"] = result.result_payload.get("error") or data.get("error_code") or "execution_failed"
                state["execution_status"] = data.get("status") or {
                    "timeout": "timed_out", "cancelled": "cancelled",
                }.get(state["error_code"], "failed")
        snapshot = snapshot.model_copy(update={"test_results": tests, "artifact_ids": artifacts,
                                                "harness_state": state})
        return await super().commit_step(step_id, result, snapshot)


async def execute_code(service, run, *, llm, strategy, test_tool, budget, execution_lease=None):
    repository = service.execution_repository
    key = str(run.id)
    async def reject(code):
        if execution_lease is not None:
            await repository.finish(execution_lease, "failed", event_type="code_task.failed",
                                    error={"code": code})
        return {"status": "failed", "error": {"code": code}, **await service._result_details(key)}

    if run.status in {"completed", "failed", "cancelled", "timed_out"}:
        return {"status": "failed", "error": {"code": "run_not_active"}, **await service._result_details(key)}
    checkpoint = await service.run_repository.get_checkpoint(run.id)
    if not checkpoint or checkpoint.state.get("protocol_version") != 1:
        return await reject("legacy_snapshot")
    profile, budget, policy = execution_config(service, run, budget)
    snapshot = await repository.get_snapshot(run.id)
    if snapshot.policy_digest != canonical_digest(policy.model_dump(mode="json")):
        return await reject("policy_mismatch")
    # Execution must use the budget frozen in the creation snapshot.
    if snapshot.budget_limits != budget.model_dump(mode="json"):
        return await reject("budget_mismatch")
    try:
        lease = execution_lease or await repository.claim(run.id, str(uuid4()), lease_seconds=service.lease_seconds)
    except RecoveryConflict as exc:
        return {"status": "failed", "error": {"code": str(exc)}, **await service._result_details(key)}
    except RuntimeError:
        return {"status": "failed", "error": {"code": "execution_claim_conflict"}, **await service._result_details(key)}
    port = CodeExecutionPort(service, lease, run.workspace_id)
    lost = asyncio.Event()
    registry = create_code_workspace_registry(service.workspace_service, service.artifact_service,
                                             service.run_repository, persist_events=False)
    if test_tool is not None:
        registry.register(test_tool)

    async def heartbeat():
        try:
            while True:
                await asyncio.sleep(service.heartbeat_seconds)
                await repository.heartbeat(lease, lease_seconds=service.lease_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            lost.set()
            execution = service._execution_tasks.get(key)
            if execution and not execution.done():
                execution.cancel()

    async def work():
        result = await service._build_harness(run, profile, test_tool).run(
            HarnessState.model_validate_json(__import__("json").dumps(snapshot.harness_state)),
            strategy, llm=llm, tools=registry, policy=policy, budget=budget,
            execution_port=port, resume_snapshot=snapshot)
        if lost.is_set() or result.stop_reason == "stale_execution_owner":
            raise StaleExecutionOwner("stale_execution_owner")
        saved = await repository.get_snapshot(run.id)
        artifact_refs = await service.artifact_service.list_for_run(run.id)
        artifact_ids = [str(ref.artifact_id) for ref in artifact_refs]
        diff_refs = [ref for ref in artifact_refs if ref.kind == "diff"]
        if diff_refs:
            service.diff_artifacts[key] = str(diff_refs[-1].artifact_id)
        state = {**saved.harness_state, "usage": result.usage.model_dump(mode="json"),
                 "tool_calls": result.tool_calls, "output": str(result.output)[:budget.max_response_chars]}
        if diff_refs:
            state["diff_artifact_id"] = str(diff_refs[-1].artifact_id)
        saved = saved.model_copy(update={"harness_state": state, "artifact_ids": artifact_ids})
        if result.status == "completed" and await service.validate_completion(
                key, str(result.output), allowed_paths=set(profile.allowed_paths)):
            state["completion_validated"] = True
            saved = saved.model_copy(update={"harness_state": state})
            await repository.finish(lease, "completed", snapshot=saved, event_type="code_task.completed")
            return {"status": "completed", "answer": result.output, **await service._result_details(key)}
        code = state.get("error_code")
        if not code:
            if result.status == "stopped":
                code = "budget_exhausted"
            elif saved.test_results and not saved.test_results[-1]["passed"]:
                code = "tests_failed"
            elif result.stop_reason == "invalid tool call":
                code = "invalid_arguments"
            elif result.stop_reason == "harness execution failed":
                code = "permission_denied" if not saved.test_results else "execution_failed"
            else:
                code = "completion_validation_failed"
        status = state.get("execution_status", "failed")
        if status not in {"failed", "cancelled", "timed_out"}:
            status = "failed"
        state.update(error_code=code, status=status)
        saved = saved.model_copy(update={"harness_state": state})
        await repository.finish(lease, status, snapshot=saved, event_type="code_task.failed",
                                event_payload={"code": code}, error={"code": code})
        return {"status": status, "error": {"code": code}, **await service._result_details(key)}

    task = asyncio.create_task(work())
    service._execution_tasks[key] = task
    pulse = None
    if execution_lease is None:
        pulse = asyncio.create_task(heartbeat())
        service._heartbeat_tasks[key] = pulse
    try:
        return await task
    except StaleExecutionOwner:
        current = await service.run_repository.get_run(run.id)
        if current is not None and current.status == "cancelled":
            return {"status": "cancelled", "error": {"code": "cancelled"},
                    **await service._result_details(key)}
        return {"status": "failed", "error": {"code": "stale_execution_owner"}, **await service._result_details(key)}
    except asyncio.CancelledError:
        if lost.is_set():
            return {"status": "failed", "error": {"code": "stale_execution_owner"}, **await service._result_details(key)}
        current = await service.run_repository.get_run(run.id)
        if current.status == "cancelled":
            return {"status": "cancelled", "error": {"code": "cancelled"}, **await service._result_details(key)}
        saved = await repository.get_snapshot(run.id)
        state = {**saved.harness_state, "error_code": "cancelled", "status": "cancelled"}
        try:
            await repository.finish(lease, "cancelled",
                snapshot=saved.model_copy(update={"harness_state": state}),
                event_type="code_task.cancelled", error={"code": "cancelled"})
        except StaleExecutionOwner:
            current = await service.run_repository.get_run(run.id)
            if current.status == "cancelled":
                return {"status": "cancelled", "error": {"code": "cancelled"},
                        **await service._result_details(key)}
            return {"status": "failed", "error": {"code": "stale_execution_owner"},
                    **await service._result_details(key)}
        raise
    finally:
        if pulse is not None:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
        service._heartbeat_tasks.pop(key, None)
        service._execution_tasks.pop(key, None)