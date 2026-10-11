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
async def test_action_requeues_same_run_and_is_idempotent(tmp_path, action, payload):
    run, repo, port, registry, gateway, provider, _ = await waiting_fixture(tmp_path)
    current = await repo.get_run(run.id)
    request = TaskActionRequest(action_id="input-1", action=action, payload=payload,
        expected_version=current.version, client_request_id="key-1")
    updated = await repo.queue_assistant_action(run.id, request)
    assert updated.id == run.id and updated.status == "queued"
    assert (await repo.queue_assistant_action(run.id, request)).version == updated.version
    with pytest.raises(RunRepositoryConflict, match="action_request_conflict"):
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