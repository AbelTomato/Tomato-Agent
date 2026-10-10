from datetime import timedelta

import pytest

from app.agent.context import ContextManager
from app.agent.harness import Harness
from app.agent.models import LLMResponse, ToolCall
from app.execution.ports import LeaseExecutionPort
from test_execution_recovery import setup_repository
from test_harness import FakeTool, RecordingLLM, ScriptedStrategy, budget, policy, state
from test_harness import FailingTool
from app.tools.registry import ToolRegistry


class Crash(BaseException):
    pass


class Port(LeaseExecutionPort):
    crash_at = None

    async def save_snapshot(self, snapshot):
        saved = await super().save_snapshot(snapshot)
        if self.crash_at == "reserve" and snapshot.in_flight_started_at is not None:
            raise Crash()
        return saved

    async def commit_model_response(self, response, snapshot, operation=None):
        saved = await super().commit_model_response(response, snapshot, operation)
        if self.crash_at == "response":
            raise Crash()
        return saved

    async def commit_step(self, step_id, result, snapshot):
        saved = await super().commit_step(step_id, result, snapshot)
        if self.crash_at == "tool":
            raise Crash()
        return saved


def harness():
    return Harness(ContextManager(token_counter=len))


async def run(port, llm, decisions, snapshot=None, **limits):
    return await harness().run(
        state(), ScriptedStrategy(decisions), llm=llm,
        tools=ToolRegistry([FakeTool()]), policy=policy(), budget=budget(**limits),
        execution_port=port, resume_snapshot=snapshot,
    )


async def make_port(tmp_path):
    repository, record, lease, clock = await setup_repository(tmp_path)
    return Port(repository, lease, "workspace"), repository, record, clock


async def test_final_response_is_saved_before_business_finish(tmp_path):
    port, repository, record, _ = await make_port(tmp_path)
    result = await run(port, RecordingLLM([LLMResponse(kind="final", content="done")]),
                       [{"kind": "respond", "output": "done"}])
    snapshot = await repository.get_snapshot(record.id)
    assert result.output == "done"
    assert snapshot.phase == "ready_finish"
    assert (await repository.get_run(record.id)).status == "running"
    llm = RecordingLLM([])
    resumed = await run(port, llm, [], snapshot)
    assert resumed.output == "done"
    assert llm.calls == 0


async def test_pending_tool_resumes_persisted_decision_and_call_id(tmp_path):
    port, repository, record, _ = await make_port(tmp_path)
    call = ToolCall(call_id="stable", name="add", arguments={"value": 8})
    port.crash_at = "response"
    with pytest.raises(Crash):
        await run(port, RecordingLLM([LLMResponse(kind="tool_call", tool_call=call)]),
                  [{"kind": "call_tool", "tool_name": "add", "arguments": {"value": 8}}])
    snapshot = await repository.get_snapshot(record.id)
    assert snapshot.phase == "pending_tool"
    operation = await port.get_operation(snapshot.pending_operation_id)
    assert operation.input_payload == {"value": 8}
    port.crash_at = None
    llm = RecordingLLM([LLMResponse(kind="final", content="done")])
    result = await run(port, llm, [{"kind": "respond", "output": "done"}], snapshot)
    assert result.status == "completed"
    assert result.usage.model_calls == 2
    assert result.tool_calls == 1
    tool_messages = [m for m in llm.message_batches[0] if m.role == "tool"]
    assert tool_messages[0].tool_call_id == "stable"
    assert "9" in tool_messages[0].content


async def test_successful_tool_is_not_replayed_after_commit(tmp_path):
    port, repository, record, _ = await make_port(tmp_path)
    port.crash_at = "tool"
    with pytest.raises(Crash):
        await run(port, RecordingLLM([LLMResponse(kind="tool_call")]),
                  [{"kind": "call_tool", "tool_name": "add", "arguments": {"value": 1}}])
    snapshot = await repository.get_snapshot(record.id)
    assert snapshot.phase == "ready_model"
    port.crash_at = None
    result = await run(port, RecordingLLM([LLMResponse(kind="final", content="done")]),
                       [{"kind": "respond", "output": "done"}], snapshot)
    assert result.tool_calls == 1
    assert (await repository.get_snapshot(record.id)).harness_state["step_index"] == 1


async def test_stale_owner_stops_before_external_call(tmp_path):
    port, repository, record, clock = await make_port(tmp_path)
    clock.value += timedelta(seconds=31)
    llm = RecordingLLM([LLMResponse(kind="final", content="done")])
    result = await run(port, llm, [{"kind": "respond", "output": "done"}])
    assert result.status == "failed"
    assert result.stop_reason == "stale_execution_owner"
    assert llm.calls == 0


async def test_crash_reservation_is_not_refunded_and_time_is_conservative(tmp_path):
    port, repository, record, _ = await make_port(tmp_path)
    port.crash_at = "reserve"
    llm = RecordingLLM([LLMResponse(kind="final", content="done")])
    with pytest.raises(Crash):
        await run(port, llm, [{"kind": "respond", "output": "done"}])
    snapshot = await repository.get_snapshot(record.id)
    assert snapshot.reserved_model_calls == 1
    assert snapshot.loop_count == 1
    assert snapshot.in_flight_max_seconds > 0
    port.crash_at = None
    result = await run(port, llm, [], snapshot)
    assert result.status == "stopped"
    assert result.stop_reason == "duration budget exhausted"
    assert llm.calls == 0


async def test_policy_change_rejects_resume_without_call(tmp_path):
    port, repository, record, _ = await make_port(tmp_path)
    port.crash_at = "response"
    with pytest.raises(Crash):
        await run(port, RecordingLLM([LLMResponse(kind="final", content="done")]),
                  [{"kind": "respond", "output": "done"}])
    snapshot = (await repository.get_snapshot(record.id)).model_copy(update={"policy_digest": "changed"})
    llm = RecordingLLM([])
    result = await run(port, llm, [], snapshot)
    assert result.stop_reason == "policy_mismatch"
    assert llm.calls == 0


async def test_successful_pending_operation_reuses_result_without_tool(tmp_path):
    port, repo, record, _ = await make_port(tmp_path)
    port.crash_at = "response"
    with pytest.raises(Crash):
        await run(port, RecordingLLM([LLMResponse(kind="tool_call")]),
                  [{"kind": "call_tool", "tool_name": "add", "arguments": {"value": 1}}])
    snapshot = await repo.get_snapshot(record.id)
    from app.execution.models import StepOutcome
    step = await port.start_step(snapshot.pending_operation_id, snapshot)
    snapshot = await port.commit_step(step.id,
        StepOutcome(success=True, result_payload={"success": True, "data": {"value": 2}, "error": None}), snapshot)
    port.crash_at = None
    result = await run(port, RecordingLLM([LLMResponse(kind="final", content="done")]),
                       [{"kind": "respond", "output": "done"}], snapshot)
    assert result.status == "completed"
    assert result.tool_calls == 0


async def test_loop_budget_is_not_reset_by_repeated_resumes(tmp_path):
    port, repo, record, _ = await make_port(tmp_path)
    port.crash_at = "response"
    for index in range(2):
        snapshot = await repo.get_snapshot(record.id) if index else None
        with pytest.raises(Crash):
            await run(port, RecordingLLM([LLMResponse(kind="tool_call")]),
                      [{"kind": "call_tool", "tool_name": "add", "arguments": {"value": index}}],
                      snapshot, max_loops=2)
    port.crash_at = None
    snapshot = await repo.get_snapshot(record.id)
    llm = RecordingLLM([])
    result = await run(port, llm, [], snapshot, max_loops=2)
    assert result.stop_reason == "loop budget exhausted"
    assert result.usage.model_calls == 2
    assert result.tool_calls == 2
    assert llm.calls == 0


async def test_operation_images_are_saved_with_validated_arguments(tmp_path):
    port, repo, record, _ = await make_port(tmp_path)
    original = port.describe_operation
    async def describe(index, name, arguments):
        operation = await original(index, name, arguments)
        return operation.model_copy(update={"before_digests": {"x": "before"},
                                             "after_digests": {"x": "after"}})
    port.describe_operation = describe
    port.crash_at = "response"
    with pytest.raises(Crash):
        await run(port, RecordingLLM([LLMResponse(kind="tool_call")]),
                  [{"kind": "call_tool", "tool_name": "add", "arguments": {"value": 1}}])
    snapshot = await repo.get_snapshot(record.id)
    operation = await port.get_operation(snapshot.pending_operation_id)
    assert operation.before_digests == {"x": "before"}
    assert operation.after_digests == {"x": "after"}
    assert operation.input_payload == {"value": 1}


async def test_failed_tool_cannot_resume_into_another_model_call(tmp_path):
    port, repo, record, _ = await make_port(tmp_path)
    result = await harness().run(state(), ScriptedStrategy([
        {"kind": "call_tool", "tool_name": "fail", "arguments": {"value": 1}}]),
        llm=RecordingLLM([LLMResponse(kind="tool_call")]), tools=ToolRegistry([FailingTool()]),
        policy=policy(allowed_tools=frozenset({"fail"})), budget=budget(), execution_port=port)
    assert result.status == "failed"
    snapshot = await repo.get_snapshot(record.id)
    llm = RecordingLLM([])
    await harness().run(state(), ScriptedStrategy([]), llm=llm, tools=ToolRegistry([FailingTool()]),
        policy=policy(allowed_tools=frozenset({"fail"})), budget=budget(),
        execution_port=port, resume_snapshot=snapshot)
    assert llm.calls == 0


async def test_owner_expiring_during_model_cannot_commit_or_call_tool(tmp_path):
    port, repo, record, clock = await make_port(tmp_path)
    class ExpiringLLM(RecordingLLM):
        async def complete(self, messages, tools):
            clock.value += timedelta(seconds=31)
            return await super().complete(messages, tools)
    result = await run(port, ExpiringLLM([LLMResponse(kind="tool_call")]),
        [{"kind": "call_tool", "tool_name": "add", "arguments": {"value": 1}}])
    assert result.stop_reason == "stale_execution_owner"
    snapshot = await repo.get_snapshot(record.id)
    assert snapshot.phase == "ready_model"
    assert snapshot.reserved_tool_calls == 0


async def test_context_and_structured_state_restore_from_snapshot(tmp_path):
    port, repo, record, _ = await make_port(tmp_path)
    port.crash_at = "response"
    original = state().model_copy(update={"structured_state": {"task": "restore me"}})
    original.context_state.summary = "saved summary"
    with pytest.raises(Crash):
        await harness().run(original, ScriptedStrategy([
            {"kind": "call_tool", "tool_name": "add", "arguments": {"value": 1}}]),
            llm=RecordingLLM([LLMResponse(kind="tool_call")]), tools=ToolRegistry([FakeTool()]),
            policy=policy(), budget=budget(), execution_port=port)
    snapshot = await repo.get_snapshot(record.id)
    port.crash_at = None
    class InspectStrategy:
        async def decide(self, restored, available, response):
            assert restored.structured_state["task"] == "restore me"
            assert restored.context_state.summary == "saved summary"
            assert restored.step_index == 1
            return {"kind": "respond", "output": "done"}
    llm = RecordingLLM([LLMResponse(kind="final", content="done")])
    result = await harness().run(state(), InspectStrategy(), llm=llm,
        tools=ToolRegistry([FakeTool()]), policy=policy(), budget=budget(),
        execution_port=port, resume_snapshot=snapshot)
    assert result.status == "completed"
    assert any(m.content == "restore me" for m in llm.message_batches[0])