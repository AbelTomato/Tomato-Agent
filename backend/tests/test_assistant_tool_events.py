import json

import pytest

from app.agent.harness import Decision
from app.agent.models import LLMResponse
from app.assistant.models import AssistantResult, CapabilityError, CapabilityOutcome, RuntimeEvent
from app.assistant.strategy import AssistantStrategy
from app.runs.repository import RunRepository
from test_assistant_harness import fixture, pending, run_harness, Strategy
from test_assistant_api import assistant_api, register, request
from test_assistant_integration import decision


async def tool_events(repo, run):
    return [event for event in await RunRepository(repo.path).list_events(run.id)
            if event.event_type.startswith("tool.")]


async def test_catalog_includes_unavailable_contract_and_preconditions(tmp_path):
    _, _, _, registry, _, _, tool = await fixture(tmp_path)
    registry._descriptors[tool.name] = registry.resolve(tool.name).model_copy(
        update={"available": False, "requires_workspace": True, "reason": "disabled"})
    strategy = AssistantStrategy()
    found = strategy.resolve_capability_decision(
        {"type": "discover_tools", "payload": {}}, registry)
    assert found["tools"][0]["contract_version"] == "1"
    assert found["tools"][0]["available"] is False
    described = strategy.resolve_capability_decision(
        {"type": "describe_tool", "payload": {"name": "check"}}, registry)
    assert described["descriptor"]["requires_workspace"] is True
    assert described["descriptor"]["reason"] == "disabled"
    assert not tool.contexts


async def test_protocol_preserves_contract_version():
    response = LLMResponse(kind="final", content=json.dumps({"type": "call_tool", "payload": {
        "name": "check", "contract_version": "old", "arguments": {}}}))
    decision = await AssistantStrategy().decide(None, (), response)
    assert decision.contract_version == "old"


@pytest.mark.parametrize("kind,category,event_type", [
    ("result", None, "tool.succeeded"),
    ("failed", "recoverable_error", "tool.failed"),
    ("failed", "permission_denied", "tool.failed"),
    ("failed", "test_failed", "tool.failed"),
    ("waiting", None, "tool.waiting"),
    ("waiting", "unknown_side_effect", "tool.unknown"),
])
async def test_tool_events_correlate_observed_outcome(tmp_path, kind, category, event_type):
    outcome = CapabilityOutcome(kind=kind,
        result=AssistantResult(answer="observed") if kind == "result" else None,
        error=CapabilityError(category=category, code="classified") if category else None,
        pending_action=pending() if kind == "waiting" else None)
    run, repo, port, registry, gateway, _, tool = await fixture(tmp_path, [outcome])
    await run_harness(repo, run, port, registry, gateway, Strategy([
        Decision(kind="call_tool", tool_name="check", arguments={}),
        Decision(kind="respond", output="done")]))
    events = await tool_events(repo, run)
    assert [event.event_type for event in events] == ["tool.started", event_type]
    parsed = [RuntimeEvent.model_validate_json(json.dumps(event.payload)) for event in events]
    assert parsed[0].invocation_id == parsed[1].invocation_id
    assert parsed[0].step_id == parsed[1].step_id
    assert all(item.event_id == event.id and item.run_id == run.id for item, event in zip(parsed, events))
    assert all(item.detail.data["tool_name"] == "check" for item in parsed)
    assert "observed" not in json.dumps([event.payload for event in events])
    assert len(tool.contexts) == 1


@pytest.mark.parametrize("gate", ["unavailable", "unknown", "workspace", "invalid_arguments", "permission"])
async def test_preflight_never_starts_tool(tmp_path, gate):
    run, repo, port, registry, gateway, _, tool = await fixture(tmp_path)
    name, arguments = "check", {}
    if gate == "unavailable":
        registry._descriptors[name] = registry.resolve(name).model_copy(update={"available": False})
    if gate == "unknown":
        registry._tools.pop(name)
    if gate == "workspace":
        registry._descriptors[name] = registry.resolve(name).model_copy(update={"requires_workspace": True})
    if gate == "invalid_arguments":
        arguments = {"value": {"secret": "not-a-string"}}
    if gate == "permission":
        name = "forbidden"
    result = await run_harness(repo, run, port, registry, gateway, Strategy([
        Decision(kind="call_tool", tool_name=name, arguments=arguments),
        Decision(kind="respond", output="done")]))
    assert result.status == ("waiting" if gate == "workspace" else "completed")
    assert not tool.contexts
    assert not await tool_events(repo, run)
    assert (await repo.get_snapshot(run.id)).reserved_tool_calls == 0


@pytest.mark.parametrize("migration", [False, True])
async def test_stale_contract_only_explicit_lossless_migration_executes(tmp_path, migration):
    run, repo, port, registry, gateway, provider, tool = await fixture(tmp_path, [
        CapabilityOutcome(kind="result", result=AssistantResult(answer="ok"))])
    if migration:
        registry.register_migration("check", "old", lambda arguments: {"value": arguments["old_value"]})
    responses = iter([
        LLMResponse(kind="final", content=json.dumps({"type": "call_tool", "payload": {
            "name": "check", "contract_version": "old", "arguments": {"old_value": "preserved"}}})),
        LLMResponse(kind="final", content=json.dumps({"type": "finish", "payload": {"result": {"answer": "done"}}})),
    ])
    async def complete(messages, tools):
        provider.calls += 1
        return next(responses)
    provider.complete = complete
    result = await run_harness(repo, run, port, registry, gateway, AssistantStrategy())
    assert result.status == "completed"
    assert len(tool.contexts) == int(migration)
    assert len(await tool_events(repo, run)) == (2 if migration else 0)
    if migration:
        assert registry.prepare_call("check", "old", {"old_value": "preserved"}) == {"value": "preserved"}
    else:
        saved = await repo.get_snapshot(run.id)
        assert saved.harness_state["structured_state"]["capability_feedback"]["error"]["code"] == "stale_contract"


@pytest.mark.parametrize("side_effect,expected", [("read_only", "tool.failed"), ("file_write", "tool.unknown")])
async def test_tool_exception_records_failure_or_unknown_without_raw_error(tmp_path, side_effect, expected):
    run, repo, port, registry, gateway, _, tool = await fixture(tmp_path)
    registry._descriptors["check"] = registry.resolve("check").model_copy(update={
        "side_effect": side_effect, "recovery_class": side_effect})
    async def execute(arguments, context):
        raise RuntimeError("credential=do-not-persist")
    tool.execute = execute
    result = await run_harness(repo, run, port, registry, gateway, Strategy([
        Decision(kind="call_tool", tool_name="check", arguments={}),
        Decision(kind="respond", output="done")]))
    assert result.status == ("waiting" if side_effect == "file_write" else "completed")
    events = await tool_events(repo, run)
    assert [item.event_type for item in events] == ["tool.started", expected]
    assert "do-not-persist" not in json.dumps([item.payload for item in events])
    if side_effect == "file_write":
        saved = await repo.get_snapshot(run.id)
        assert (await repo.get_operation(saved.pending_operation_id)).status == "unknown"


async def test_tool_result_and_event_rollback_together(tmp_path, monkeypatch):
    run, repo, port, registry, gateway, _, tool = await fixture(tmp_path, [
        CapabilityOutcome(kind="result", result=AssistantResult(answer="ok"))])
    original = repo._append_event
    async def fail_result(db, run_id, event_type, *args, **kwargs):
        if event_type == "tool.succeeded":
            raise RuntimeError("event-store-failure")
        return await original(db, run_id, event_type, *args, **kwargs)
    monkeypatch.setattr(repo, "_append_event", fail_result)
    with pytest.raises(RuntimeError, match="event-store-failure"):
        await run_harness(repo, run, port, registry, gateway, Strategy([
            Decision(kind="call_tool", tool_name="check", arguments={})]))
    saved = await repo.get_snapshot(run.id)
    assert (await repo.get_operation(saved.pending_operation_id)).status == "running"
    assert [item.event_type for item in await tool_events(repo, run)] == ["tool.started"]


async def test_path_permission_rejected_before_started_event(tmp_path):
    async with assistant_api(tmp_path, [
        decision("call_tool", {"name": "read_file", "contract_version": "1", "arguments": {"path": "../secret"}}),
        decision("finish", {"result": {"answer": "Access denied"}}),
    ]) as (app, client, session, _):
        workspace = await register(app, tmp_path)
        run_id = (await client.post(f"/api/sessions/{session}/tasks",
            json=request(workspace_id=workspace.workspace_id))).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        assert (await client.get(f"/api/tasks/{run_id}")).json()["status"] == "completed"
        events = (await client.get(f"/api/tasks/{run_id}/events")).json()["events"]
        assert not [item for item in events if item["event_type"].startswith("tool.")]


async def test_catalog_message_tool_finish_api_sequence(tmp_path):
    async with assistant_api(tmp_path, [
        decision("message", {"text": "准备读取"}),
        decision("discover_tools", {"category": "project"}),
        decision("describe_tool", {"name": "read_file"}),
        decision("call_tool", {"name": "read_file", "contract_version": "1", "arguments": {"path": "cache.py"}}),
        decision("finish", {"result": {"answer": "done"}}),
    ]) as (app, client, session, _):
        workspace = await register(app, tmp_path)
        run_id = (await client.post(f"/api/sessions/{session}/tasks",
            json=request(workspace_id=workspace.workspace_id))).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        assert (await client.get(f"/api/tasks/{run_id}")).json()["status"] == "completed"
        events = (await client.get(f"/api/tasks/{run_id}/events")).json()["events"]
        assert [item["event_type"] for item in events if item["event_type"] in {
            "assistant.message", "tool.started", "tool.succeeded"}] == ["assistant.message", "tool.started", "tool.succeeded"]


async def test_expired_running_tool_records_unknown_once(tmp_path):
    from datetime import timedelta
    from app.execution.models import OperationInput
    from test_execution_recovery import Clock
    from uuid import uuid4

    run, repo, port, _, _, _, _ = await fixture(tmp_path)
    clock = Clock()
    clock.value = repo._aware_now()
    repo.clock = clock
    snapshot = await repo.get_snapshot(run.id)
    operation = OperationInput(id=uuid4(), logical_index=0, kind="tool", tool_name="check",
        input_payload={}, recovery_class="read_only")
    snapshot = snapshot.model_copy(update={"phase": "pending_tool", "pending_operation_id": operation.id})
    await repo.prepare_operation(port.lease, operation, snapshot)
    step = await repo.start_step(port.lease, operation.id, snapshot)
    clock.value += timedelta(seconds=60)
    await repo.expire_and_classify(port.lease.attempt_id)
    await repo.expire_and_classify(port.lease.attempt_id)
    events = await tool_events(repo, run)
    assert [item.event_type for item in events] == ["tool.started", "tool.unknown"]
    assert events[-1].payload["step_id"] == str(step.id)
    assert events[-1].payload["invocation_id"] == str(operation.id)


async def test_committed_result_recovery_does_not_duplicate_events(tmp_path):
    from app.execution.models import OperationInput, StepOutcome
    from uuid import uuid4
    run, repo, port, registry, gateway, _, tool = await fixture(tmp_path)
    snap = await repo.get_snapshot(run.id)
    operation = OperationInput(id=uuid4(), logical_index=0, kind="tool", tool_name="check",
        input_payload={}, recovery_class="read_only")
    snap = snap.model_copy(update={"phase": "pending_tool", "pending_operation_id": operation.id})
    await repo.prepare_operation(port.lease, operation, snap)
    step = await repo.start_step(port.lease, operation.id, snap)
    result = CapabilityOutcome(kind="result", result=AssistantResult(answer="persisted"))
    from app.tools.base import ToolResult
    await repo.commit_step(port.lease, step.id, StepOutcome(success=True,
        result_payload=ToolResult(success=True, data=result.model_dump(mode="json")).model_dump(mode="json")), snap)
    await run_harness(repo, run, port, registry, gateway,
        Strategy([Decision(kind="respond", output="done")]))
    assert not tool.contexts
    assert [item.event_type for item in await tool_events(repo, run)] == ["tool.started", "tool.succeeded"]