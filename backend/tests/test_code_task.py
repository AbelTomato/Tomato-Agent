from uuid import UUID
import hashlib

import pytest

from app.agent.code_task import CodeTaskService
from app.agent.harness_models import Budget
from app.agent.models import ContextState, LLMResponse
from app.agent.harness_models import HarnessState
from app.agent.models import CodeTaskRequest
from app.agent.policies import CodeTaskCapabilityProfile


async def save_completion_evidence(service, run, lease, *, passed=True, exit_code=0):
    diff = await service.workspace_service.diff(run.workspace_id)
    artifact = await service.artifact_service.register_text(run.id, "diff", diff)
    snapshot = await service.execution_repository.get_snapshot(run.id)
    tests = [*snapshot.test_results, {
        "target": "unit", "passed": passed, "exit_code": exit_code,
        "workspace_diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
    }]
    await service.execution_repository.save_snapshot(lease, snapshot.model_copy(update={
        "test_results": tests,
        "harness_state": {**snapshot.harness_state, "diff_artifact_id": str(artifact.artifact_id)},
        "artifact_ids": [*snapshot.artifact_ids, str(artifact.artifact_id)],
    }))


@pytest.mark.asyncio
async def test_create_builds_server_owned_profile_and_separate_run(tmp_path):
    service = CodeTaskService.for_testing(tmp_path)

    run = await service.create(CodeTaskRequest(task="Fix the failing test"))
    profile = service.build_capability_profile(run)

    assert isinstance(run.id, UUID)
    assert run.status == "queued"
    assert await service.execution_repository.get_snapshot(run.id) is not None
    assert profile.allow_network is False
    assert profile.allow_process is False
    assert set(profile.allowed_tools) >= {"read_file", "write_file", "run_tests"}
    assert profile.allowed_paths == (str((service.workspace_service.root / run.workspace_id / "repo").resolve()),)


@pytest.mark.asyncio
async def test_build_capability_profile_uses_injected_limits(tmp_path):
    service = CodeTaskService.for_testing(tmp_path)
    service.capability_profile_factory = lambda repo: CodeTaskCapabilityProfile(
        allowed_paths=(str(repo.resolve()),), timeout_seconds=3.5, max_output_chars=123
    )

    run = await service.create(CodeTaskRequest(task="Use configured limits"))
    profile = service.build_capability_profile(run)

    assert profile.timeout_seconds == 3.5
    assert profile.max_output_chars == 123
    assert profile.allowed_paths == (str((service.workspace_service.root / run.workspace_id / "repo").resolve()),)


@pytest.mark.asyncio
async def test_completion_requires_approved_diff_passing_test_and_readable_artifacts(tmp_path):
    service = CodeTaskService.for_testing(tmp_path)
    run = await service.create(CodeTaskRequest(task="Fix the failing test"))

    assert await service.validate_completion(str(run.id)) is False
    lease = await service.execution_repository.claim(run.id, "completion-test", lease_seconds=30)

    await service.workspace_service.write_file(run.workspace_id, "src.py", "value = 1\n")
    await service.artifact_service.register_text(run.id, "test_report", '{"passed": true}')
    await save_completion_evidence(service, run, lease)

    assert await service.validate_completion(str(run.id)) is True
    assert (await service.run_repository.get_run(run.id)).status == "running"


@pytest.mark.asyncio
async def test_completion_rejects_workspace_changes_after_passing_test(tmp_path):
    service = CodeTaskService.for_testing(tmp_path)
    run = await service.create(CodeTaskRequest(task="Fix the failing test"))
    lease = await service.execution_repository.claim(run.id, "completion-test", lease_seconds=30)

    await service.workspace_service.write_file(run.workspace_id, "src.py", "value = 1\n")
    await save_completion_evidence(service, run, lease)
    await service.workspace_service.write_file(run.workspace_id, "src.py", "value = 2\n")

    assert await service.validate_completion(str(run.id)) is False
    assert (await service.run_repository.get_run(run.id)).status == "running"


@pytest.mark.asyncio
async def test_execute_passes_server_allowed_paths_to_completion_validation(tmp_path):
    service = CodeTaskService.for_testing(tmp_path)
    run = await service.create(CodeTaskRequest(task="Fix the failing test"))
    calls = 0

    class LLM:
        async def complete(self, messages, tools):
            return LLMResponse(kind="tool_call")

    class Strategy:
        async def decide(self, state, tools):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"kind": "call_tool", "tool_name": "write_file",
                        "arguments": {"path": "src.py", "content": "value = 1\n"}}
            if calls == 2:
                return {"kind": "call_tool", "tool_name": "run_tests",
                        "arguments": {"target": "unit"}}
            if calls == 3:
                return {"kind": "call_tool", "tool_name": "get_diff", "arguments": {}}
            return {"kind": "respond", "output": "done"}

    class TestTool:
        name = "run_tests"
        description = "test"

        from pydantic import BaseModel

        class input_model(BaseModel):
            target: str

        def definition(self):
            from app.agent.models import ToolDefinition
            return ToolDefinition(name=self.name, description=self.description,
                                  parameters={"type": "object"})

        async def execute(self, arguments, context):
            from app.tools.base import ToolResult
            return ToolResult(success=True, data={"target": "unit", "passed": True, "exit_code": 0})

    result = await service.execute(str(run.id), llm=LLM(), strategy=Strategy(), test_tool=TestTool())

    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_failed_tests_cannot_be_overridden_by_model_response(tmp_path):
    service = CodeTaskService.for_testing(tmp_path)
    run = await service.create(CodeTaskRequest(task="Fix the failing test"))
    lease = await service.execution_repository.claim(run.id, "completion-test", lease_seconds=30)
    await save_completion_evidence(service, run, lease, passed=False, exit_code=1)

    result = await service.validate_completion(str(run.id), proposed_answer="Done, all tests pass")

    assert result is False
    assert (await service.run_repository.get_run(run.id)).status == "running"


@pytest.mark.asyncio
async def test_harness_retests_consume_separate_turns_and_record_each_attempt(tmp_path):
    from pydantic import BaseModel

    from app.agent.models import ToolDefinition
    from app.tools.base import ToolResult

    service = CodeTaskService.for_testing(tmp_path)
    run = await service.create(CodeTaskRequest(task="Fix the failing test"))
    calls = 0

    class Arguments(BaseModel):
        target: str

    class TestTool:
        name = "run_tests"
        description = "test"
        input_model = Arguments

        def definition(self):
            return ToolDefinition(name=self.name, description=self.description,
                                  parameters=Arguments.model_json_schema())

        async def execute(self, arguments, context):
            nonlocal calls
            assert (await service.run_repository.get_run(run.id)).status == "running"
            calls += 1
            return ToolResult(success=True, data={
                "target": arguments["target"], "passed": False, "exit_code": 1,
            })

    class LLM:
        async def complete(self, messages, tools):
            return LLMResponse(kind="tool_call")

    class Strategy:
        async def decide(self, state, tools):
            if calls >= 3:
                return {"kind": "stop", "reason": "tests exhausted"}
            return {"kind": "call_tool", "tool_name": "run_tests",
                    "arguments": {"target": "unit"}}

    result = await service.execute(str(run.id), llm=LLM(), strategy=Strategy(), test_tool=TestTool())

    assert calls == 3
    assert result["error"]["code"] == "budget_exhausted"
    snapshot = await service.execution_repository.get_snapshot(run.id)
    assert len(snapshot.test_results) == 3
    assert snapshot.reserved_tool_calls == 3
    assert all(not result["passed"] for result in snapshot.test_results)
    events = await service.run_repository.list_events(run.id)
    assert sum(event.event_type == "step.succeeded" for event in events) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_error, expected_status, expected_code", [
    ("backend_unavailable", "failed", "backend_unavailable"),
    ("permission_denied", "failed", "permission_denied"),
    ("invalid_arguments", "failed", "invalid_arguments"),
    ("timeout", "timed_out", "timeout"),
])
async def test_harness_persists_test_tool_errors(tmp_path, tool_error, expected_status, expected_code):
    from pydantic import BaseModel

    from app.agent.models import ToolDefinition
    from app.tools.base import ToolResult

    service = CodeTaskService.for_testing(tmp_path)
    run = await service.create(CodeTaskRequest(task="Run tests"))

    class Arguments(BaseModel):
        target: str

    class TestTool:
        name = "run_tests"
        description = "test"
        input_model = Arguments

        def definition(self):
            return ToolDefinition(name=self.name, description=self.description,
                                  parameters=Arguments.model_json_schema())

        async def execute(self, arguments, context):
            return ToolResult(success=False, error=tool_error)

    class LLM:
        async def complete(self, messages, tools):
            return LLMResponse(kind="tool_call")

    class Strategy:
        async def decide(self, state, tools):
            return {"kind": "call_tool", "tool_name": "run_tests",
                    "arguments": {"target": "unit"}}

    await service.execute(str(run.id), llm=LLM(), strategy=Strategy(), test_tool=TestTool())
    persisted = await service.run_repository.get_run(run.id)
    checkpoint = await service.run_repository.get_checkpoint(run.id)
    assert persisted.status == expected_status
    assert checkpoint.state["harness_state"]["error_code"] == expected_code


@pytest.mark.asyncio
@pytest.mark.parametrize("status, error_code, expected_status", [
    ("timed_out", "timeout", "timed_out"),
    ("cancelled", "cancelled", "cancelled"),
    ("failed", "backend_unavailable", "failed"),
])
async def test_real_run_tests_execution_status_is_terminal_without_retry(
    tmp_path, status, error_code, expected_status
):
    from pydantic import BaseModel

    from app.agent.models import ToolDefinition
    from app.tools.base import ToolResult

    service = CodeTaskService.for_testing(tmp_path)
    run = await service.create(CodeTaskRequest(task="Run tests"))
    calls = 0

    class Arguments(BaseModel):
        target: str

    class TestTool:
        name = "run_tests"
        description = "test"
        input_model = Arguments

        def definition(self):
            return ToolDefinition(name=self.name, description=self.description,
                                  parameters=Arguments.model_json_schema())

        async def execute(self, arguments, context):
            nonlocal calls
            calls += 1
            return ToolResult(success=False, data={
                "target": arguments["target"], "status": status,
                "passed": False, "exit_code": None, "error_code": error_code,
            }, error=error_code)

    class LLM:
        async def complete(self, messages, tools):
            return LLMResponse(kind="tool_call")

    class Strategy:
        async def decide(self, state, tools):
            return {"kind": "call_tool", "tool_name": "run_tests",
                    "arguments": {"target": "unit"}}

    await service.execute(str(run.id), llm=LLM(), strategy=Strategy(), test_tool=TestTool())

    persisted = await service.run_repository.get_run(run.id)
    checkpoint = await service.run_repository.get_checkpoint(run.id)
    assert calls == 1
    assert persisted.status == expected_status
    assert persisted.error == {"code": error_code}
    assert checkpoint.state["harness_state"]["error_code"] == error_code
    assert checkpoint.state["harness_state"]["status"] == expected_status


@pytest.mark.asyncio
async def test_harness_invalid_arguments_are_terminal_without_running_tool(tmp_path):
    from pydantic import BaseModel

    from app.agent.models import ToolDefinition
    from app.tools.base import ToolResult

    service = CodeTaskService.for_testing(tmp_path)
    run = await service.create(CodeTaskRequest(task="Run tests"))
    calls = 0

    class Arguments(BaseModel):
        target: str

    class TestTool:
        name = "run_tests"
        description = "test"
        input_model = Arguments

        def definition(self):
            return ToolDefinition(name=self.name, description=self.description,
                                  parameters=Arguments.model_json_schema())

        async def execute(self, arguments, context):
            nonlocal calls
            calls += 1
            return ToolResult(success=True, data={"passed": True, "exit_code": 0})

    class LLM:
        async def complete(self, messages, tools):
            return LLMResponse(kind="tool_call")

    class Strategy:
        async def decide(self, state, tools):
            return {"kind": "call_tool", "tool_name": "run_tests", "arguments": {"target": 3}}

    await service.execute(str(run.id), llm=LLM(), strategy=Strategy(), test_tool=TestTool())
    persisted = await service.run_repository.get_run(run.id)
    checkpoint = await service.run_repository.get_checkpoint(run.id)
    assert calls == 0
    assert persisted.status == "failed"
    assert checkpoint.state["harness_state"]["error_code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_cancelled_test_tool_marks_run_cancelled_and_propagates(tmp_path):
    import asyncio

    from pydantic import BaseModel

    from app.agent.models import ToolDefinition
    from app.tools.base import ToolResult

    service = CodeTaskService.for_testing(tmp_path)
    run = await service.create(CodeTaskRequest(task="Run tests"))

    class Arguments(BaseModel):
        target: str

    class TestTool:
        name = "run_tests"
        description = "test"
        input_model = Arguments

        def definition(self):
            return ToolDefinition(name=self.name, description=self.description,
                                  parameters=Arguments.model_json_schema())

        async def execute(self, arguments, context):
            raise asyncio.CancelledError

    class LLM:
        async def complete(self, messages, tools):
            return LLMResponse(kind="tool_call")

    class Strategy:
        async def decide(self, state, tools):
            return {"kind": "call_tool", "tool_name": "run_tests",
                    "arguments": {"target": "unit"}}

    with pytest.raises(asyncio.CancelledError):
        await service.execute(str(run.id), llm=LLM(), strategy=Strategy(), test_tool=TestTool())
    persisted = await service.run_repository.get_run(run.id)
    checkpoint = await service.run_repository.get_checkpoint(run.id)
    assert persisted.status == "cancelled"
    assert checkpoint.state["harness_state"]["error_code"] == "cancelled"
    assert checkpoint.state["harness_state"]["status"] == "cancelled"
    restarted = CodeTaskService.for_testing(tmp_path)
    await restarted.initialize()
    assert (await restarted.run_repository.get_run(run.id)).status == "cancelled"
    assert (await restarted.execution_repository.latest_attempt(run.id)).status == "cancelled"
    assert not service._execution_tasks
    assert not service._heartbeat_tasks


@pytest.mark.asyncio
async def test_budget_exhaustion_is_terminal_and_repeat_execution_is_rejected(tmp_path):
    service = CodeTaskService.for_testing(tmp_path)
    service.budget = Budget(max_loops=1, max_tool_calls=1, max_duration_seconds=1,
                           max_context_tokens=100, max_response_chars=100)
    run = await service.create(CodeTaskRequest(task="Fix the failing test"))

    class LLM:
        async def complete(self, messages, tools):
            return LLMResponse(kind="tool_call")

    class Strategy:
        async def decide(self, state, tools):
            return {"kind": "stop", "reason": "budget"}

    result = await service.execute(str(run.id), llm=LLM(), strategy=Strategy())

    assert result["status"] == "failed"
    assert result["error"]["code"] == "budget_exhausted"
    assert {"usage", "tool_calls", "events", "artifacts", "test_results",
            "diff_artifact", "changed_files"} <= result.keys()
    assert (await service.run_repository.get_run(run.id)).status == "failed"
    repeated = await service.execute(str(run.id), llm=LLM(), strategy=Strategy())
    assert repeated["error"]["code"] == "run_not_active"


@pytest.mark.asyncio
async def test_completion_rejects_out_of_scope_diff_and_duplicate_completion(tmp_path):
    service = CodeTaskService.for_testing(tmp_path)
    run = await service.create(CodeTaskRequest(task="Fix the failing test"))
    lease = await service.execution_repository.claim(run.id, "completion-test", lease_seconds=30)
    await service.workspace_service.write_file(run.workspace_id, "secret.py", "bad = True\n")

    await save_completion_evidence(service, run, lease)
    assert await service.validate_completion(str(run.id), allowed_paths={"src.py"}) is False
    await service.workspace_service.write_file(run.workspace_id, "src.py", "ok = True\n")
    await save_completion_evidence(service, run, lease)
    assert await service.validate_completion(str(run.id), allowed_paths={"src.py", "secret.py"}) is True
    snapshot = await service.execution_repository.get_snapshot(run.id)
    await service.execution_repository.finish(lease, "completed", snapshot=snapshot,
                                               event_type="code_task.completed")
    assert await service.validate_completion(str(run.id), allowed_paths={"src.py", "secret.py"}) is False