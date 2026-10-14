import pytest

from app.agent.harness import Decision
from app.tasks.models import TaskActionRequest
from app.runs.models import RunRepositoryConflict
from test_assistant_harness import fixture, pending, run_harness, Strategy


async def waiting_fixture(tmp_path):
    values = await fixture(tmp_path)
    run, repo, port, registry, gateway, _, _ = values
    await run_harness(repo, run, port, registry, gateway,
                      Strategy([Decision(kind="wait", pending_action=pending())]))
    return values


@pytest.mark.parametrize("action,payload", [("provide_input", {"message": "details"}), ("reject_action", {})])
async def test_action_requeues_same_run_and_is_consumed_once(tmp_path, action, payload):
    run, repo, port, registry, gateway, provider, _ = await waiting_fixture(tmp_path)
    current = await repo.get_run(run.id)
    request = TaskActionRequest(action_id="input-1", action=action, payload=payload,
        expected_version=current.version, client_request_id="key-1")
    updated = await repo.queue_assistant_action(run.id, request)
    assert updated.id == run.id and updated.status == "queued"
    with pytest.raises(RunRepositoryConflict, match="stale_action"):
        await repo.queue_assistant_action(run.id, request)
    with pytest.raises(RunRepositoryConflict, match="stale_action"):
        await repo.queue_assistant_action(run.id, request.model_copy(update={"payload": {"message": "changed"}}))
    lease = await repo.claim(run.id, "new-owner", lease_seconds=30)
    port.lease = lease
    strategy = Strategy([Decision(kind="respond", output="continued")])
    assert (await run_harness(repo, run, port, registry, gateway, strategy)).status == "completed"
    assert strategy.states[0].structured_state["action_feedback"]["action"] == action
    assert provider.calls == 2


async def test_stale_action_cannot_resume(tmp_path):
    run, repo, *_ = await waiting_fixture(tmp_path)
    current = await repo.get_run(run.id)
    request = TaskActionRequest(action_id="input-1", action="provide_input", payload={"message": "details"},
        expected_version=current.version - 1, client_request_id="key-1")
    with pytest.raises(RunRepositoryConflict, match="stale_action"):
        await repo.queue_assistant_action(run.id, request)
    assert (await repo.get_run(run.id)).status == "waiting"


async def test_context_deduplicates_domain_history_and_isolates_sessions(tmp_path):
    from app.assistant.context import build_assistant_context
    from test_unified_tasks_api import api
    from uuid import UUID
    async with api(tmp_path) as (app, _, session):
        sessions = app.state.session_repository
        runs = app.state.run_repository
        domain = await sessions.create_run(UUID(session))
        await sessions.append_event(UUID(session), domain, "user_message", {"content": "old question"})
        await sessions.append_event(UUID(session), domain, "assistant_message", {"content": "old answer"})
        await sessions.update_run(domain, "completed")
        unified = await runs.create_session_run(UUID(session), "chat", {"message": "old question"}, None, "old")
        unified = await runs.update_status(unified.id, "running", expected_version=unified.version)
        await runs.update_status(unified.id, "completed", expected_version=unified.version,
            result={"answer": "old answer"}, domain_run_id=str(domain))
        other = await sessions.create_session()
        foreign = await sessions.create_run(other)
        await sessions.append_event(other, foreign, "user_message", {"content": "secret other session"})
        await sessions.update_run(foreign, "completed")
        state = await build_assistant_context(UUID(session), "new question", None,
            {"sessions": sessions, "runs": runs})
        history = state.structured_state["history"]
        assert [item["content"] for item in history] == ["old question", "old answer"]
        assert state.structured_state["task"] == "new question"


async def test_rejected_action_is_not_asked_again(tmp_path):
    run, repo, port, registry, gateway, _, _ = await waiting_fixture(tmp_path)
    current = await repo.get_run(run.id)
    await repo.queue_assistant_action(run.id, TaskActionRequest(action_id="input-1", action="reject_action",
        payload={}, expected_version=current.version, client_request_id="reject"))
    port.lease = await repo.claim(run.id, "new", lease_seconds=30)
    result = await run_harness(repo, run, port, registry, gateway,
        Strategy([Decision(kind="wait", pending_action=pending())]))
    assert result.status == "failed" and result.stop_reason == "rejected_action_repeated"


async def test_approval_requires_server_validator_and_preserves_frozen_action(tmp_path):
    from app.assistant.models import CapabilityOutcome
    from app.agent.harness import Decision
    action = pending().model_copy(update={"action_type": "confirmation", "target_capability": "check",
        "allowed_actions": ["approve_action", "reject_action"], "frozen_parameters": {"value": "frozen"}})
    run, repo, port, registry, gateway, _, tool = await fixture(tmp_path,
        [CapabilityOutcome(kind="waiting", pending_action=action)])
    await run_harness(repo, run, port, registry, gateway,
        Strategy([Decision(kind="call_tool", tool_name="check", arguments={})]))
    current = await repo.get_run(run.id)
    request = TaskActionRequest(action_id=action.action_id, action="approve_action", payload={},
        expected_version=current.version, client_request_id="approve")
    with pytest.raises(RunRepositoryConflict, match="confirmation_handler_unavailable"):
        await repo.queue_assistant_action(run.id, request)
    seen = []
    def validate(pending_action, payload):
        seen.append(pending_action.frozen_parameters)
        assert payload == {}
    await repo.queue_assistant_action(run.id, request, confirmation_validator=validate)
    assert seen == [{"value": "frozen"}] and len(tool.contexts) == 1
    assert (await repo.get_snapshot(run.id)).harness_state["structured_state"]["action_feedback"]["frozen_parameters"] == {"value": "frozen"}


async def test_protocol_question_freezes_choices_and_accepts_only_valid_answer_forms(tmp_path):
    from app.assistant.strategy import AssistantStrategy
    from app.agent.models import LLMResponse
    import json
    values = await fixture(tmp_path)
    run, repo, port, registry, gateway, *_ = values
    decision = await AssistantStrategy().decide(None, [], LLMResponse(kind="final", content=json.dumps({
        "protocol_version": 1, "type": "ask_user", "payload": {
            "question": "Which?", "choices": ["A", "B"], "allow_free_text": True}})))
    await run_harness(repo, run, port, registry, gateway, Strategy([decision]))
    current = await repo.get_run(run.id)
    assert current.pending_action.frozen_parameters == {"choices": ["A", "B"], "allow_free_text": True}
    assert current.pending_action.allowed_actions == ["provide_input", "cancel"]
    request = TaskActionRequest(action_id=current.pending_action.action_id, action="provide_input",
        payload={"choice": "missing"}, expected_version=current.version, client_request_id="invalid")
    with pytest.raises(ValueError, match="invalid_answer"):
        await repo.queue_assistant_action(run.id, request)
    assert (await repo.get_run(run.id)).status == "waiting"
    await repo.queue_assistant_action(run.id, request.model_copy(update={"payload": {"choice": "A"}}))


async def unknown_fixture(tmp_path):
    values = await fixture(tmp_path)
    run, repo, port, registry, gateway, _, tool = values
    registry._descriptors["check"] = registry.resolve("check").model_copy(update={
        "side_effect": "file_write", "recovery_class": "file_write"})
    async def fail(arguments, context):
        tool.contexts.append(context)
        raise RuntimeError("unobserved write")
    tool.execute = fail
    result = await run_harness(repo, run, port, registry, gateway,
        Strategy([Decision(kind="call_tool", tool_name="check", arguments={})]))
    assert result.status == "waiting"
    return values


@pytest.mark.parametrize("resolution", ["executed", "not_executed", "unknown"])
async def test_unknown_side_effect_checks_are_independent_facts(tmp_path, resolution):
    from app.execution.repository import ExecutionRepository
    from app.runs.repository import RunRepository
    run, repo, _, _, _, _, tool = await unknown_fixture(tmp_path)
    repo = ExecutionRepository(repo.path)
    current = await repo.get_run(run.id)
    original = await repo.get_snapshot(run.id)
    operation = await repo.get_operation(original.pending_operation_id)
    assert current.pending_action.frozen_parameters["invocation_id"] == str(operation.id)
    request = TaskActionRequest(action_id=current.pending_action.action_id, action="resolve_side_effect",
        payload={"resolution": resolution}, expected_version=current.version, client_request_id="check")
    updated = await repo.queue_assistant_action(run.id, request)
    assert updated.status == ("waiting" if resolution == "unknown" else "queued")
    assert (await repo.get_operation(operation.id)).model_dump() == operation.model_dump()
    assert len(tool.contexts) == 1
    if resolution == "unknown":
        assert updated.pending_action.action_id != current.pending_action.action_id
    with pytest.raises(RunRepositoryConflict, match="stale_action"):
        await repo.queue_assistant_action(run.id, request)
    events = await RunRepository(repo.path).list_events(run.id)
    checks = [e for e in events if e.event_type == "tool.reconciled"]
    assert len(checks) == 1 and checks[0].payload["invocation_id"] == str(operation.id)
    assert checks[0].payload["detail"]["data"]["resolution"] == resolution


async def test_concurrent_answer_only_resumes_once(tmp_path):
    import asyncio
    from app.runs.repository import RunRepository
    run, repo, *_ = await waiting_fixture(tmp_path)
    current = await repo.get_run(run.id)
    request = TaskActionRequest(action_id="input-1", action="provide_input", payload={"message": "answer"},
        expected_version=current.version, client_request_id="answer")
    results = await asyncio.gather(repo.queue_assistant_action(run.id, request),
        repo.queue_assistant_action(run.id, request), return_exceptions=True)
    assert sum(isinstance(result, RunRepositoryConflict) for result in results) == 1
    events = await RunRepository(repo.path).list_events(run.id)
    assert len([e for e in events if e.event_type == "assistant.resumed"]) == 1


async def test_cancelled_late_tool_result_only_appends_audit(tmp_path):
    from app.runs.repository import RunRepository
    from app.execution.models import StepOutcome
    from app.tools.base import ToolResult
    import aiosqlite
    run, repo, port, *_ = await unknown_fixture(tmp_path)
    snap = await repo.get_snapshot(run.id)
    async with aiosqlite.connect(repo.path) as db:
        step = (await db.execute_fetchall("SELECT id FROM execution_steps WHERE operation_id=?",
            (str(snap.pending_operation_id),)))[0][0]
    await repo.request_cancel(run.id)
    original = await repo.get_operation(snap.pending_operation_id)
    await repo.record_assistant_late_result(port.lease, step, StepOutcome(success=True,
        result_payload=ToolResult(success=True, data={}).model_dump(mode="json")))
    assert (await repo.get_run(run.id)).status == "cancelled"
    assert await repo.get_snapshot(run.id) == snap
    assert await repo.get_operation(original.id) == original
    assert (await RunRepository(repo.path).list_events(run.id))[-1].event_type == "tool.late_result"


async def test_unknown_side_effect_blocks_conflicting_start(tmp_path):
    from app.execution.recovery import RecoveryConflict
    run, repo, port, *_ = await unknown_fixture(tmp_path)
    with pytest.raises(RecoveryConflict, match="unknown_side_effect"):
        await repo.assert_assistant_effect_allowed(run.id)


async def test_settlement_without_termination_evidence_keeps_quarantine(tmp_path):
    import aiosqlite
    run, repo, *_ = await unknown_fixture(tmp_path)
    current = await repo.get_run(run.id)
    invocation = (await repo.get_snapshot(run.id)).pending_operation_id
    await repo.queue_assistant_action(run.id, TaskActionRequest(
        action_id=current.pending_action.action_id, action="resolve_side_effect",
        payload={"resolution": "executed"}, expected_version=current.version, client_request_id="settle"))
    async with aiosqlite.connect(repo.path) as db:
        rows = await db.execute_fetchall("SELECT resolution,quarantined,termination_evidence,conflict "
            "FROM assistant_effect_settlements WHERE invocation_id=?", (str(invocation),))
    assert rows == [("executed", 1, None, 0)]
    assert (await repo.get_operation(invocation)).status == "unknown"
    with pytest.raises(RunRepositoryConflict, match="termination_evidence_missing"):
        await repo.reconcile_effect_quarantine(run.id, invocation)
    assert (await repo.list_effect_settlements(run.id))[0]["quarantined"] is True


@pytest.mark.parametrize("evidence", ["writer-stopped", "risk_accepted", "", None,
    '{"schema_version":1,"source":"user_report"}',
    '{"schema_version":1,"source":"docker_daemon","run_id":"other","objects":[]}'])
async def test_settlement_rejects_unstructured_termination_evidence(tmp_path, evidence):
    run, repo, *_ = await unknown_fixture(tmp_path)
    current = await repo.get_run(run.id)
    invocation = (await repo.get_snapshot(run.id)).pending_operation_id
    await repo.queue_assistant_action(run.id, TaskActionRequest(
        action_id=current.pending_action.action_id, action="resolve_side_effect",
        payload={"resolution": "executed"}, expected_version=current.version, client_request_id="settle"))
    async def unstructured(_run_id, _invocation_id):
        return evidence
    with pytest.raises(RunRepositoryConflict, match="termination_evidence_missing"):
        await repo.reconcile_effect_quarantine(run.id, invocation, termination_verifier=unstructured)
    assert (await repo.list_effect_settlements(run.id))[0]["quarantined"] is True


async def test_settlement_rejects_docker_evidence_with_incomplete_step_identity(tmp_path):
    import aiosqlite
    run, repo, *_ = await unknown_fixture(tmp_path)
    current = await repo.get_run(run.id)
    invocation = (await repo.get_snapshot(run.id)).pending_operation_id
    async with aiosqlite.connect(repo.path) as db:
        await db.execute("UPDATE execution_operations SET tool_name='run_tests',recovery_class='sandbox' WHERE id=?",
            (str(invocation),))
        step = (await db.execute_fetchall("SELECT id FROM execution_steps WHERE operation_id=?", (str(invocation),)))[0][0]
        await db.execute("INSERT INTO docker_tool_executions "
            "(step_id,run_id,invocation_id,container_name,daemon_id,launch_closed,termination_evidence) "
            "VALUES (?,?,?,?,?,1,?)", (step, str(run.id), str(invocation), "container", "daemon-1", "{}"))
        await db.commit()
    await repo.queue_assistant_action(run.id, TaskActionRequest(
        action_id=current.pending_action.action_id, action="resolve_side_effect",
        payload={"resolution": "executed"}, expected_version=current.version, client_request_id="settle-step"))
    evidence = {"schema_version": 1, "source": "docker_daemon", "run_id": str(run.id),
        "invocation_id": str(invocation), "daemon_id": "daemon-1", "objects": []}

    async def verifier(_run_id, _invocation_id):
        return evidence

    with pytest.raises(RunRepositoryConflict, match="termination_evidence_missing"):
        await repo.reconcile_effect_quarantine(run.id, invocation, termination_verifier=verifier)
    assert (await repo.list_effect_settlements(run.id))[0]["quarantined"] is True


async def test_settlement_atomically_persists_matching_docker_evidence(tmp_path):
    import aiosqlite
    import json
    import pytest
    run, repo, *_ = await unknown_fixture(tmp_path)
    current = await repo.get_run(run.id)
    invocation = (await repo.get_snapshot(run.id)).pending_operation_id
    async with aiosqlite.connect(repo.path) as db:
        await db.execute("UPDATE execution_operations SET tool_name='run_tests',recovery_class='sandbox' WHERE id=?",
            (str(invocation),))
        step = (await db.execute_fetchall("SELECT id FROM execution_steps WHERE operation_id=?", (str(invocation),)))[0][0]
        await db.execute("INSERT INTO docker_tool_executions "
            "(step_id,run_id,invocation_id,container_name,daemon_id,launch_closed) VALUES (?,?,?,?,?,1)",
            (step, str(run.id), str(invocation), "container", "daemon-1"))
        await db.commit()
    await repo.queue_assistant_action(run.id, TaskActionRequest(
        action_id=current.pending_action.action_id, action="resolve_side_effect",
        payload={"resolution": "executed"}, expected_version=current.version, client_request_id="settle-valid"))
    evidence = {"schema_version": 1, "source": "docker_daemon", "run_id": str(run.id),
        "invocation_id": str(invocation), "daemon_id": "daemon-1",
        "objects": [{"step_id": step, "container_name": "container", "state": "absent"}]}

    async def verifier(verified_run_id, verified_invocation_id):
        assert str(verified_run_id) == str(run.id)
        assert str(verified_invocation_id) == str(invocation)
        return evidence

    await repo.reconcile_effect_quarantine(run.id, invocation, termination_verifier=verifier)
    projection = (await repo.list_effect_settlements(run.id))[0]
    assert projection["quarantined"] is False
    assert json.loads(projection["termination_evidence"]) == evidence
    async with aiosqlite.connect(repo.path) as db:
        docker_rows = await db.execute_fetchall("SELECT lifecycle_state,termination_evidence "
            "FROM docker_tool_executions WHERE invocation_id=?", (str(invocation),))
    assert docker_rows == [("terminated", json.dumps(evidence, sort_keys=True, separators=(",", ":")))]
    with pytest.raises(RunRepositoryConflict, match="effect_already_reconciled"):
        await repo.reconcile_effect_quarantine(run.id, invocation, termination_verifier=verifier)


async def test_docker_late_success_cannot_release_quarantine_or_rewrite_unknown_operation(tmp_path):
    import aiosqlite
    from app.execution.models import StepOutcome
    run, repo, port, *_ = await unknown_fixture(tmp_path)
    current = await repo.get_run(run.id)
    invocation = (await repo.get_snapshot(run.id)).pending_operation_id
    async with aiosqlite.connect(repo.path) as db:
        await db.execute("UPDATE execution_operations SET tool_name='run_tests',recovery_class='sandbox' WHERE id=?",
            (str(invocation),))
        step = (await db.execute_fetchall("SELECT id FROM execution_steps WHERE operation_id=?",
            (str(invocation),)))[0][0]
        await db.execute("INSERT INTO docker_tool_executions "
            "(step_id,run_id,invocation_id,container_name,daemon_id,launch_closed) VALUES (?,?,?,?,?,0)",
            (step, str(run.id), str(invocation), "late-container", "trusted"))
        await db.commit()
    await repo.queue_assistant_action(run.id, TaskActionRequest(
        action_id=current.pending_action.action_id, action="resolve_side_effect",
        payload={"resolution": "executed"}, expected_version=current.version, client_request_id="docker-settle"))
    await repo.request_cancel(run.id)
    original = await repo.get_operation(invocation)
    await repo.record_assistant_late_result(port.lease, step, StepOutcome(success=True, result_payload={"exit_code": 0}))
    projection = (await repo.list_effect_settlements(run.id))[0]
    assert projection["quarantined"] and projection["termination_evidence"] is None
    assert await repo.get_operation(invocation) == original
    async with aiosqlite.connect(repo.path) as db:
        rows = await db.execute_fetchall("SELECT launch_closed,termination_evidence FROM docker_tool_executions")
    assert rows == [(0, None)]


async def test_user_risk_acceptance_allows_new_run_without_claiming_termination(tmp_path):
    import aiosqlite
    from app.runs.repository import RunRepository
    from app.sessions.repository import SessionRepository
    from app.agent.harness import Decision
    from test_assistant_harness import Strategy, run_harness
    run, repo, port, registry, gateway, *_ = await unknown_fixture(tmp_path)
    current = await repo.get_run(run.id)
    invocation = (await repo.get_snapshot(run.id)).pending_operation_id
    await repo.queue_assistant_action(run.id, TaskActionRequest(
        action_id=current.pending_action.action_id, action="resolve_side_effect",
        payload={"resolution": "executed", "accept_risk": True},
        expected_version=current.version, client_request_id="accept-risk"))
    projection = (await repo.list_effect_settlements(run.id))[0]
    assert projection == {"invocation_id": str(invocation), "resolution": "executed", "quarantined": True,
        "termination_evidence": None, "conflict": False, "risk_accepted": True, "admission_blocked": False}
    async with aiosqlite.connect(repo.path) as db:
        accepted = await db.execute_fetchall("SELECT risk_accepted,quarantined,termination_evidence "
            "FROM assistant_effect_settlements WHERE invocation_id=?", (str(invocation),))
    assert accepted == [(1, 1, None)]
    await repo.assert_assistant_effect_allowed(run.id)
    with pytest.raises(RunRepositoryConflict, match="termination_evidence_missing"):
        await repo.reconcile_effect_quarantine(run.id, invocation)
    lease = await repo.claim(run.id, "risk-accepted-owner", lease_seconds=30)
    port.lease = lease
    result = await run_harness(repo, run, port, registry, gateway,
        Strategy([Decision(kind="respond", output="continued")]))
    assert result.status == "completed"
    runs = RunRepository(repo.path)
    await runs.init()
    sessions = SessionRepository(repo.path)
    await sessions.init()
    session_id = await sessions.create_session()
    new_run = await runs.create_session_run(session_id, "assistant", {"message": "continue"},
        workspace_id="workspace-1",
        client_request_id="risk-accepted-run")
    assert new_run.status == "queued"


async def test_late_success_conflicts_with_not_executed_report_and_retains_lock(tmp_path):
    import aiosqlite
    from app.execution.models import StepOutcome
    run, repo, port, *_ = await unknown_fixture(tmp_path)
    current = await repo.get_run(run.id)
    invocation = (await repo.get_snapshot(run.id)).pending_operation_id
    await repo.queue_assistant_action(run.id, TaskActionRequest(
        action_id=current.pending_action.action_id, action="resolve_side_effect",
        payload={"resolution": "not_executed"}, expected_version=current.version, client_request_id="check"))
    await repo.request_cancel(run.id)
    async with aiosqlite.connect(repo.path) as db:
        step = (await db.execute_fetchall("SELECT id FROM execution_steps WHERE operation_id=?",
            (str(invocation),)))[0][0]
    await repo.record_assistant_late_result(port.lease, step, StepOutcome(success=True, result_payload={}))
    projection = (await repo.list_effect_settlements(run.id))[0]
    assert projection["resolution"] == "not_executed" and projection["conflict"] and projection["quarantined"]
    with pytest.raises(RunRepositoryConflict, match="effect_unresolved"):
        await repo.reconcile_effect_quarantine(run.id, invocation)


async def test_risk_acceptance_is_rejected_after_late_conflict(tmp_path):
    import aiosqlite
    from app.execution.models import StepOutcome
    from app.execution.recovery import RecoveryConflict
    run, repo, port, *_ = await unknown_fixture(tmp_path)
    current = await repo.get_run(run.id)
    invocation = (await repo.get_snapshot(run.id)).pending_operation_id
    await repo.queue_assistant_action(run.id, TaskActionRequest(action_id=current.pending_action.action_id,
        action="resolve_side_effect", payload={"resolution": "not_executed"},
        expected_version=current.version, client_request_id="record-report"))
    await repo.request_cancel(run.id)
    async with aiosqlite.connect(repo.path) as db:
        step = (await db.execute_fetchall("SELECT id FROM execution_steps WHERE operation_id=?", (str(invocation),)))[0][0]
    await repo.record_assistant_late_result(port.lease, step, StepOutcome(success=True, result_payload={}))
    projection = (await repo.list_effect_settlements(run.id))[0]
    assert projection["conflict"] and projection["quarantined"] and not projection["risk_accepted"]
    assert projection["admission_blocked"] is True
    await repo.accept_effect_conflict_risk(run.id, invocation)
    accepted = (await repo.list_effect_settlements(run.id))[0]
    assert accepted["conflict"] and accepted["quarantined"]
    assert accepted["risk_accepted"] and not accepted["admission_blocked"]
    await repo.assert_assistant_effect_allowed(run.id)
    with pytest.raises(RunRepositoryConflict, match="effect_unresolved"):
        await repo.reconcile_effect_quarantine(run.id, invocation)


async def test_late_success_revokes_previously_accepted_conflict_risk(tmp_path):
    import aiosqlite
    from app.execution.models import StepOutcome
    from app.execution.recovery import RecoveryConflict
    run, repo, port, *_ = await unknown_fixture(tmp_path)
    current = await repo.get_run(run.id)
    invocation = (await repo.get_snapshot(run.id)).pending_operation_id
    await repo.queue_assistant_action(run.id, TaskActionRequest(action_id=current.pending_action.action_id,
        action="resolve_side_effect", payload={"resolution": "not_executed"},
        expected_version=current.version, client_request_id="record-before-late"))
    await repo.request_cancel(run.id)
    async with aiosqlite.connect(repo.path) as db:
        step = (await db.execute_fetchall("SELECT id FROM execution_steps WHERE operation_id=?", (str(invocation),)))[0][0]
    await repo.record_assistant_late_result(port.lease, step, StepOutcome(success=True, result_payload={}))
    await repo.accept_effect_conflict_risk(run.id, invocation, "accept-before-later-result")
    await repo.record_assistant_late_result(port.lease, step, StepOutcome(success=True, result_payload={"late": True}))
    settlement = (await repo.list_effect_settlements(run.id))[0]
    assert settlement["conflict"] and settlement["quarantined"]
    assert not settlement["risk_accepted"] and settlement["admission_blocked"]
    with pytest.raises(RecoveryConflict, match="unknown_side_effect"):
        await repo.assert_assistant_effect_allowed(run.id)


async def test_effect_conflict_risk_acceptance_requires_conflict_and_unblocks_admission(tmp_path):
    run, repo, port, *_ = await unknown_fixture(tmp_path)
    invocation = (await repo.get_snapshot(run.id)).pending_operation_id
    with pytest.raises(RunRepositoryConflict, match="effect_conflict_required"):
        await repo.accept_effect_conflict_risk(run.id, invocation)


async def test_confirmed_not_executed_retries_only_under_current_contract(tmp_path):
    from app.assistant.models import AssistantResult, CapabilityOutcome
    from app.tools.base import ToolResult
    run, repo, port, registry, gateway, _, tool = await unknown_fixture(tmp_path)
    snap = await repo.get_snapshot(run.id)
    invocation = snap.pending_operation_id
    current = await repo.get_run(run.id)
    action = TaskActionRequest(action_id=current.pending_action.action_id, action="resolve_side_effect",
        payload={"resolution": "not_executed"}, expected_version=current.version, client_request_id="retry")
    await repo.queue_assistant_action(run.id, action, recovery_validator=lambda pending, op: True)
    assert (await repo.get_snapshot(run.id)).phase == "pending_tool"
    port.lease = await repo.claim(run.id, "retry-owner", lease_seconds=30)
    async def succeed(arguments, context):
        tool.contexts.append(context)
        return ToolResult(success=True,
            data=CapabilityOutcome(kind="result", result=AssistantResult(answer="saved")).model_dump(mode="json"))
    tool.execute = succeed
    result = await run_harness(repo, run, port, registry, gateway,
        Strategy([Decision(kind="respond", output="done")]))
    assert result.status == "completed" and len(tool.contexts) == 2
    assert (await repo.get_operation(invocation)).status == "succeeded"
    import aiosqlite
    async with aiosqlite.connect(repo.path) as db:
        rows = await db.execute_fetchall("SELECT status FROM execution_steps WHERE operation_id=? ORDER BY execution_no",
            (str(invocation),))
    assert rows == [("unknown",), ("succeeded",)]