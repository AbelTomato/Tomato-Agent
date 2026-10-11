import json

import pytest
from pydantic import BaseModel

from app.agent.harness import Decision, Harness
from app.agent.harness_models import Budget, HarnessState, ToolPolicy
from app.agent.models import ContextState, LLMResponse, ToolDefinition
from app.assistant.models import AssistantResult, CapabilityDescriptor, CapabilityError, CapabilityOutcome
from app.execution.repository import ExecutionRepository
from app.runs.repository import RunRepository
from app.runs.models import PendingAction
from app.tools.base import ToolResult
from test_assistant_persistence import assistant_snapshot


class Input(BaseModel):
    value: str = "ok"


class Tool:
    name = "check"
    input_model = Input

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.contexts = []

    def definition(self):
        return ToolDefinition(name=self.name, description="check", parameters=Input.model_json_schema())

    async def execute(self, arguments, context):
        self.contexts.append(context)
        outcome = self.outcomes.pop(0)
        return ToolResult(success=outcome.kind == "result", data=outcome.model_dump(mode="json"))


class Provider:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        return LLMResponse(kind="final", content="next")


class Strategy:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.states = []

    async def decide(self, state, available, response):
        self.states.append(state)
        return next(self.decisions)


def pending():
    return PendingAction(action_id="input-1", reason="clarify", business_version=1,
                         action_type="clarification", allowed_actions=["provide_input", "reject_action"])


async def fixture(tmp_path, outcomes=()):
    from app.assistant.capabilities import CapabilityRegistry
    from app.assistant.execution import AssistantExecutionPort
    from app.assistant.model_gateway import RunModelGateway
    runs = RunRepository(tmp_path / "assistant.db")
    await runs.init()
    run = await runs.create_run("assistant", {"message": "goal"}, None)
    repo = ExecutionRepository(runs.path)
    await repo.init()
    state = HarnessState(task_id=str(run.id), context_state=ContextState(), structured_state={"task": "goal"})
    snap = assistant_snapshot(run.id, harness_state=state.model_dump(mode="json"),
        budget_limits={"max_model_calls": 12, "max_execution_seconds": 20.0}, loop_count=0)
    await repo.bootstrap_snapshot(snap)
    lease = await repo.claim(run.id, "owner", lease_seconds=30)
    tool = Tool(outcomes)
    registry = CapabilityRegistry()
    registry.register(tool, CapabilityDescriptor(name="check", parameters=Input.model_json_schema(),
        requires_workspace=False, side_effect="read_only", recovery_class="read_only", available=True))
    port = AssistantExecutionPort(repo, lease, None, capabilities=registry, profile="server-profile")
    provider = Provider()
    gateway = RunModelGateway(provider, port, config_digest="config", timeout_seconds=2.0)
    return run, repo, port, registry, gateway, provider, tool


async def run_harness(repo, run, port, registry, gateway, strategy):
    snap = await repo.get_snapshot(run.id)
    return await Harness().run(HarnessState.model_validate_json(json.dumps(snap.harness_state)), strategy,
        llm=gateway, tools=registry, policy=ToolPolicy(allowed_tools=frozenset({"check"}),
            max_calls=8, timeout_seconds=1.0, max_result_chars=5000),
        budget=Budget(max_loops=12, max_tool_calls=8, max_duration_seconds=20.0,
            max_context_tokens=4000, max_response_chars=2000), execution_port=port, resume_snapshot=snap)


@pytest.mark.parametrize("category", ["test_failed", "recoverable_error", "permission_denied", "unavailable"])
async def test_classified_failure_is_feedback_then_legal_alternative(tmp_path, category):
    failed = CapabilityOutcome(kind="failed", error=CapabilityError(category=category, code=category),
        result=AssistantResult(validation_status="failed" if category == "test_failed" else "unavailable"))
    run, repo, port, registry, gateway, provider, tool = await fixture(tmp_path, [failed])
    strategy = Strategy([Decision(kind="call_tool", tool_name="check", arguments={}),
                         Decision(kind="respond", output="explained")])
    result = await run_harness(repo, run, port, registry, gateway, strategy)
    assert result.status == "completed"
    assert strategy.states[-1].structured_state["capability_feedback"]["error"]["category"] == category
    assert provider.calls == (await repo.get_snapshot(run.id)).reserved_model_calls == 2
    assert tool.contexts[0].session_id == str((await repo.get_snapshot(run.id)).session_id)
    assert tool.contexts[0].profile == "server-profile"
    assert 0 < tool.contexts[0].remaining_seconds <= 1.0


async def test_wait_persists_and_releases_owner(tmp_path):
    run, repo, port, registry, gateway, _, _ = await fixture(tmp_path)
    result = await run_harness(repo, run, port, registry, gateway,
        Strategy([Decision(kind="wait", pending_action=pending())]))
    assert result.status == "waiting"
    saved = await repo.get_snapshot(run.id)
    current = await repo.get_run(run.id)
    assert saved.phase == "waiting" and saved.pending_action == current.pending_action == pending()
    assert current.status == "waiting"
    assert (await repo.latest_attempt(run.id)).status == "released"


@pytest.mark.parametrize("category,status", [("ownership_lost", "failed"), ("internal_error", "failed")])
async def test_terminal_failure_does_not_call_model_again(tmp_path, category, status):
    outcome = CapabilityOutcome(kind="failed", error=CapabilityError(category=category, code=category))
    run, repo, port, registry, gateway, provider, _ = await fixture(tmp_path, [outcome])
    result = await run_harness(repo, run, port, registry, gateway,
        Strategy([Decision(kind="call_tool", tool_name="check", arguments={})]))
    assert result.status == status and provider.calls == 1


async def test_unknown_side_effect_waits_without_reexecution(tmp_path):
    outcome = CapabilityOutcome(kind="waiting", pending_action=pending().model_copy(update={
        "action_type": "recovery", "allowed_actions": ["cancel", "inspect_recovery"]}),
        error=CapabilityError(category="unknown_side_effect", code="unknown"))
    run, repo, port, registry, gateway, provider, tool = await fixture(tmp_path, [outcome])
    result = await run_harness(repo, run, port, registry, gateway,
        Strategy([Decision(kind="call_tool", tool_name="check", arguments={})]))
    assert result.status == "waiting" and len(tool.contexts) == provider.calls == 1
    assert (await repo.get_snapshot(run.id)).pending_operation_id is not None


def test_registry_rejects_missing_or_mismatched_descriptor():
    from app.assistant.capabilities import CapabilityRegistry
    registry = CapabilityRegistry()
    with pytest.raises(ValueError):
        registry.register(Tool([]), None)
    with pytest.raises(ValueError):
        registry.register(Tool([]), CapabilityDescriptor(name="other", parameters={},
            requires_workspace=False, side_effect="file_write", recovery_class="read_only", available=True))


async def test_failed_test_can_be_repaired_and_retested(tmp_path):
    failure = CapabilityOutcome(kind="failed", error=CapabilityError(category="test_failed", code="assertion"),
        result=AssistantResult(validation_status="failed", test_results=[{"passed": False}]))
    success = CapabilityOutcome(kind="result", result=AssistantResult(validation_status="passed", test_results=[{"passed": True}]))
    run, repo, port, registry, gateway, _, tool = await fixture(tmp_path, [failure, success])
    strategy = Strategy([Decision(kind="call_tool", tool_name="check", arguments={}),
        Decision(kind="call_tool", tool_name="check", arguments={"value": "fixed"}),
        Decision(kind="respond", output="done")])
    assert (await run_harness(repo, run, port, registry, gateway, strategy)).status == "completed"
    assert len(tool.contexts) == 2
    assert (await repo.get_snapshot(run.id)).test_results == [{"passed": False}, {"passed": True}]


async def test_permission_denied_does_not_repeat_tool_execution(tmp_path):
    failure = CapabilityOutcome(kind="failed", error=CapabilityError(category="permission_denied", code="denied"))
    run, repo, port, registry, gateway, _, tool = await fixture(tmp_path, [failure])
    strategy = Strategy([Decision(kind="call_tool", tool_name="check", arguments={}),
        Decision(kind="call_tool", tool_name="check", arguments={"value": "different"}),
        Decision(kind="respond", output="stopped")])
    assert (await run_harness(repo, run, port, registry, gateway, strategy)).status == "completed"
    assert len(tool.contexts) == 1


async def test_invalid_parameters_are_feedback_not_terminal(tmp_path):
    success = CapabilityOutcome(kind="result", result=AssistantResult(answer="ok"))
    run, repo, port, registry, gateway, _, tool = await fixture(tmp_path, [success])
    strategy = Strategy([Decision(kind="call_tool", tool_name="check", arguments={"value": []}),
        Decision(kind="call_tool", tool_name="check", arguments={"value": "corrected"}),
        Decision(kind="respond", output="done")])
    result = await run_harness(repo, run, port, registry, gateway, strategy)
    assert result.status == "completed"
    assert strategy.states[1].structured_state["capability_feedback"]["error"]["category"] == "recoverable_error"
    assert len(tool.contexts) == 1


async def test_tool_wait_is_persisted_and_history_reaches_model(tmp_path):
    outcome = CapabilityOutcome(kind="waiting", pending_action=pending())
    run, repo, port, registry, gateway, provider, _ = await fixture(tmp_path, [outcome])
    snap = await repo.get_snapshot(run.id)
    state = dict(snap.harness_state)
    state["structured_state"]["history"] = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "previous"}]
    await repo.save_snapshot(port.lease, snap.model_copy(update={"harness_state": state}))
    observed = []
    original = provider.complete
    async def capture(messages, tools):
        observed.extend(messages)
        return await original(messages, tools)
    provider.complete = capture
    result = await run_harness(repo, run, port, registry, gateway,
        Strategy([Decision(kind="call_tool", tool_name="check", arguments={})]))
    assert result.status == "waiting"
    assert "earlier" in [message.content for message in observed]


async def test_stale_owner_cannot_execute_tool_or_publish_wait(tmp_path):
    run, repo, port, registry, gateway, provider, tool = await fixture(tmp_path)
    await repo.request_cancel(run.id)
    result = await run_harness(repo, run, port, registry, gateway,
        Strategy([Decision(kind="wait", pending_action=pending())]))
    assert result.stop_reason == "stale_execution_owner"
    assert provider.calls == 0 and not tool.contexts


async def test_tool_inflight_timeout_and_cancellation_share_run_limits(tmp_path):
    import asyncio
    run, repo, port, registry, gateway, _, tool = await fixture(tmp_path)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    async def execute(arguments, context):
        snap = await repo.get_snapshot(run.id)
        assert snap.in_flight_max_seconds == context.remaining_seconds
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    tool.execute = execute
    task = asyncio.create_task(run_harness(repo, run, port, registry, gateway,
        Strategy([Decision(kind="call_tool", tool_name="check", arguments={})])))
    await asyncio.wait_for(started.wait(), 2)
    port.cancel_event.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert (await repo.get_snapshot(run.id)).reserved_tool_calls == 1


async def test_execute_assistant_commits_result_on_same_run(tmp_path):
    from app.assistant.execution import execute_assistant
    run, repo, port, registry, gateway, _, _ = await fixture(tmp_path)
    snap = await repo.get_snapshot(run.id)
    run = run.model_copy(update={"session_id": snap.session_id})
    result = await execute_assistant(run, port.lease, port, harness=Harness(),
        strategy=Strategy([Decision(kind="respond", output="answer")]), llm=gateway,
        policy=ToolPolicy(allowed_tools=frozenset({"check"}), max_calls=8, timeout_seconds=1.0, max_result_chars=5000),
        budget=Budget(max_loops=12, max_tool_calls=8, max_duration_seconds=20.0,
            max_context_tokens=4000, max_response_chars=2000))
    assert result.status == "completed"
    assert (await repo.get_run(run.id)).result == {"output": "answer"}


async def test_strategy_cannot_grant_confirmation(tmp_path):
    run, repo, port, registry, gateway, _, _ = await fixture(tmp_path)
    result = await run_harness(repo, run, port, registry, gateway,
        Strategy([Decision(kind="wait", pending_action=pending().model_copy(update={"action_type": "confirmation"}))]))
    assert result.stop_reason == "strategy_cannot_authorize_business_action"