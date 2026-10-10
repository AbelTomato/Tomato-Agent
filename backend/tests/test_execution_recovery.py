import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import aiosqlite
import pytest

from app.execution.models import ExecutionSnapshot
from app.execution.ports import canonical_digest
from app.execution.recovery import (
    RecoveryObservation, RecoveryOperation, classify_recovery, RecoveryConflict,
)
from app.execution.repository import ExecutionRepository
from app.runs.repository import RunRepository


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 10, tzinfo=timezone.utc)

    def now(self):
        return self.value


def snapshot(run_id):
    return ExecutionSnapshot(
        run_id=run_id, schema_version=1, checkpoint_revision=1,
        last_event_sequence=0, phase="ready_model", logical_index=0,
        messages=[], harness_state={}, budget_limits={"max_model_calls": 5},
        reserved_model_calls=1, reserved_tool_calls=1, loop_count=1,
        elapsed_upper_bound_seconds=2.0, policy_digest="policy",
        workspace_id="workspace", test_results=[], artifact_ids=[],
    )


def operation(kind="file_write", status="unknown"):
    payload = {"path": "x"}
    return RecoveryOperation(
        id=uuid4(), logical_index=0, kind="tool", tool_name="write_file",
        recovery_class=kind, status=status, input_payload=payload,
        input_digest=canonical_digest(payload),
        before_digests={"a": "old-a", "b": "old-b"},
        after_digests={"a": "new-a", "b": "new-b"},
        result_payload={"ok": True}, output_digest="output",
    )


@pytest.mark.parametrize("kind,status,observation,action", [
    ("read_only", "succeeded", {}, "reuse"),
    ("model", "unknown", {}, "retry"),
    ("read_only", "unknown", {}, "retry"),
    ("file_write", "unknown", {}, "manual_review"),
    ("file_write", "unknown", {"executor_exited": True,
        "file_digests": {"a": "new-a", "b": "new-b"}}, "reconcile"),
    ("file_write", "unknown", {"executor_exited": True,
        "file_digests": {"a": "old-a", "b": "old-b"}}, "retry"),
    ("file_write", "unknown", {"executor_exited": True,
        "file_digests": {"a": "new-a", "b": "old-b"}}, "manual_review"),
    ("file_write", "unknown", {"executor_exited": True,
        "file_digests": {"a": "other", "b": "new-b"}}, "manual_review"),
    ("file_write", "unknown", {"file_digests": {
        "a": "new-a", "b": "new-b"}}, "manual_review"),
    ("sandbox", "unknown", {}, "manual_review"),
    ("sandbox", "unknown", {"executor_exited": True,
        "sandbox_reconciled": True, "sandbox_retry_approved": True}, "retry"),
])
def test_recovery_rules(kind, status, observation, action):
    snap = snapshot(uuid4())
    op = operation(kind, status)
    decision = classify_recovery(snap, op, RecoveryObservation(
        policy_digest="policy", **observation))
    assert decision.action == action


@pytest.mark.parametrize("count,action", [(0, "manual_review"), (1, "reconcile"), (2, "manual_review")])
def test_artifact_requires_unique_operation_and_content_match(count, action):
    op = operation("artifact")
    matches = [{"artifact_id": str(i), "operation_id": str(op.id),
                "content_digest": "output"} for i in range(count)]
    decision = classify_recovery(snapshot(uuid4()), op, RecoveryObservation(
        policy_digest="policy", artifact_refs=matches))
    assert decision.action == action


def test_unrelated_artifact_is_not_reused():
    op = operation("artifact")
    observation = RecoveryObservation(policy_digest="policy", artifact_refs=[{
        "artifact_id": "x", "operation_id": str(uuid4()), "content_digest": "output"}])
    assert classify_recovery(snapshot(uuid4()), op, observation).action == "manual_review"


@pytest.mark.parametrize("change,code", [
    ({"policy_digest": "changed"}, "policy_mismatch"),
    ({"protocol_version": 2}, "unsupported_protocol"),
    ({"schema_version": 2}, "unsupported_snapshot"),
    ({"messages": None}, "invalid_snapshot"),
])
def test_invalid_recovery_snapshot_is_rejected(change, code):
    raw = snapshot(uuid4()).model_dump(mode="json")
    raw.update(change)
    decision = classify_recovery(raw, None, RecoveryObservation(policy_digest="policy"))
    assert decision.action == "reject"
    assert decision.reason_code == code


def test_legacy_snapshot_and_input_digest_conflict_are_rejected():
    obs = RecoveryObservation(policy_digest="policy")
    assert classify_recovery(None, None, obs).reason_code == "legacy_snapshot"
    op = operation("read_only", "succeeded").model_copy(update={"input_digest": "changed"})
    assert classify_recovery(snapshot(uuid4()), op, obs).reason_code == "operation_input_conflict"


def test_persisted_model_response_continues_without_model_retry():
    from app.agent.models import LLMResponse, ToolCall
    snap = snapshot(uuid4()).model_copy(update={"phase": "pending_tool",
        "pending_response": LLMResponse(kind="tool_call", tool_call=ToolCall(name="read_file", arguments={}))})
    assert classify_recovery(snap, None, RecoveryObservation(policy_digest="policy")).action == "reuse"


async def setup_repository(tmp_path):
    path = tmp_path / "execution.db"
    runs = RunRepository(path)
    await runs.init()
    run = await runs.create_run("code_task", {}, "workspace")
    clock = Clock()
    repository = ExecutionRepository(path, clock=clock)
    await repository.init()
    lease = await repository.claim(run.id, "owner", lease_seconds=30)
    await repository.commit_checkpoint(lease, snapshot(run.id).model_dump(mode="json"))
    return repository, run, lease, clock


async def seed_operation(repository, run, lease, op):
    async with aiosqlite.connect(repository.path) as db:
        await db.execute(
            "INSERT INTO execution_operations (id, run_id, logical_index, kind, tool_name, "
            "input_digest, input_payload, recovery_class, status, result_payload, "
            "before_digests, after_digests, output_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(op.id), str(run.id), op.logical_index, op.kind, op.tool_name,
             op.input_digest, json.dumps(op.input_payload), op.recovery_class, op.status,
             json.dumps(op.result_payload), json.dumps(op.before_digests),
             json.dumps(op.after_digests), op.output_digest))
        await db.execute(
            "INSERT INTO execution_steps (id, operation_id, attempt_id, execution_no, status, started_at) "
            "VALUES (?, ?, ?, 1, 'running', ?)",
            (str(uuid4()), str(op.id), str(lease.attempt_id), clock_time()))
        await db.execute("UPDATE task_run_checkpoints SET state=json_set(state, '$.pending_operation_id', ?) "
                         "WHERE run_id=?", (str(op.id), str(run.id)))
        await db.commit()


def clock_time():
    return datetime(2026, 10, 10, tzinfo=timezone.utc).isoformat()


async def test_scan_is_read_only_and_rechecks_renewed_lease(tmp_path):
    repository, run, lease, clock = await setup_repository(tmp_path)
    clock.value += timedelta(seconds=29)
    assert await repository.scan_expired(10) == []
    await repository.heartbeat(lease, lease_seconds=30)
    clock.value += timedelta(seconds=2)
    with pytest.raises(RecoveryConflict):
        await repository.expire_and_classify(lease.attempt_id)
    assert (await repository.get_attempt(lease.attempt_id)).status == "running"


async def test_safe_recovery_requeues_and_fences_old_owner(tmp_path):
    repository, run, lease, clock = await setup_repository(tmp_path)
    clock.value += timedelta(seconds=31)
    assert len(await repository.scan_expired(10)) == 1
    assert (await repository.get_attempt(lease.attempt_id)).status == "running"
    candidate = await repository.expire_and_classify(lease.attempt_id)
    with pytest.raises(RecoveryConflict):
        await repository.claim(run.id, "premature", lease_seconds=30)
    decision = classify_recovery(candidate.snapshot, candidate.operation,
                                 RecoveryObservation(policy_digest="policy"))
    assert (await repository.apply_recovery(candidate, decision)).status == "queued"
    new = await repository.claim(run.id, "new-owner", lease_seconds=30)
    assert new.fencing_token > lease.fencing_token
    assert new.attempt_id != lease.attempt_id
    with pytest.raises(RuntimeError):
        await repository.commit_checkpoint(lease, {})
    assert (await repository.get_snapshot(run.id)).reserved_model_calls == 1


async def test_unknown_write_waits_then_reconciles_without_replaying(tmp_path):
    repository, run, lease, clock = await setup_repository(tmp_path)
    op = operation(status="running")
    await seed_operation(repository, run, lease, op)
    clock.value += timedelta(seconds=31)
    candidate = await repository.expire_and_classify(lease.attempt_id)
    assert candidate.operation.status == "unknown"
    decision = classify_recovery(candidate.snapshot, candidate.operation,
                                 RecoveryObservation(policy_digest="policy"))
    assert (await repository.apply_recovery(candidate, decision)).status == "waiting"
    async with aiosqlite.connect(repository.path) as db:
        await repository._append_event(db, run.id, "audit.observed", {}, clock.now())
        await db.commit()
    with pytest.raises(RecoveryConflict):
        await repository.claim(run.id, "unsafe", lease_seconds=30)
    candidate = await repository.get_recovery_candidate(run.id)
    decision = classify_recovery(candidate.snapshot, candidate.operation, RecoveryObservation(
        policy_digest="policy", executor_exited=True, file_digests=op.after_digests))
    assert (await repository.apply_recovery(candidate, decision)).status == "queued"
    async with aiosqlite.connect(repository.path) as db:
        assert (await (await db.execute("SELECT status FROM execution_operations")).fetchone())[0] == "succeeded"
        assert (await (await db.execute("SELECT status FROM execution_steps")).fetchone())[0] == "unknown"


@pytest.mark.parametrize("mutation", ["cancel", "checkpoint", "finish"])
async def test_recovery_cannot_overwrite_changed_candidate(tmp_path, mutation):
    repository, run, lease, clock = await setup_repository(tmp_path)
    clock.value += timedelta(seconds=31)
    candidate = await repository.expire_and_classify(lease.attempt_id)
    decision = classify_recovery(candidate.snapshot, candidate.operation,
                                 RecoveryObservation(policy_digest="policy"))
    if mutation == "cancel":
        await repository.request_cancel(run.id)
    else:
        async with aiosqlite.connect(repository.path) as db:
            if mutation == "checkpoint":
                await db.execute("UPDATE task_run_checkpoints SET sequence=sequence+1 WHERE run_id=?", (str(run.id),))
            else:
                await db.execute("UPDATE task_runs SET status='completed' WHERE id=?", (str(run.id),))
            await db.commit()
    with pytest.raises(RecoveryConflict):
        await repository.apply_recovery(candidate, decision)


async def test_recovery_transaction_failure_rolls_back(tmp_path, monkeypatch):
    repository, run, lease, clock = await setup_repository(tmp_path)
    clock.value += timedelta(seconds=31)

    async def fail(_db):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(repository, "_commit", fail)
    with pytest.raises(RuntimeError, match="injected failure"):
        await repository.expire_and_classify(lease.attempt_id)
    assert (await repository.get_attempt(lease.attempt_id)).status == "running"
    assert (await repository.get_run(run.id)).status == "running"


async def test_terminal_run_cannot_be_claimed_or_cancelled_again(tmp_path):
    repository, run, lease, _ = await setup_repository(tmp_path)
    await repository.finish(lease)
    assert (await repository.request_cancel(run.id)).status == "completed"
    with pytest.raises(RecoveryConflict):
        await repository.claim(run.id, "new-owner", lease_seconds=30)


async def test_apply_failure_preserves_operation_checkpoint_and_events(tmp_path, monkeypatch):
    repository, run, lease, clock = await setup_repository(tmp_path)
    op = operation("read_only", "running")
    await seed_operation(repository, run, lease, op)
    clock.value += timedelta(seconds=31)
    candidate = await repository.expire_and_classify(lease.attempt_id)
    decision = classify_recovery(candidate.snapshot, candidate.operation,
                                 RecoveryObservation(policy_digest="policy"))

    async def fail(_db):
        raise RuntimeError("apply failed")

    monkeypatch.setattr(repository, "_commit", fail)
    with pytest.raises(RuntimeError, match="apply failed"):
        await repository.apply_recovery(candidate, decision)
    assert await repository.get_recovery_candidate(run.id) == candidate
    runs = RunRepository(repository.path)
    assert (await runs.list_events(run.id))[-1].event_type == "execution.expired"


async def test_missing_snapshot_rejected_without_changing_run(tmp_path):
    repository, run, lease, clock = await setup_repository(tmp_path)
    async with aiosqlite.connect(repository.path) as db:
        await db.execute("DELETE FROM task_run_checkpoints WHERE run_id=?", (str(run.id),))
        await db.commit()
    clock.value += timedelta(seconds=31)
    candidate = await repository.expire_and_classify(lease.attempt_id)
    decision = classify_recovery(candidate.snapshot, None, RecoveryObservation(policy_digest="policy"))
    assert decision.reason_code == "legacy_snapshot"
    assert (await repository.apply_recovery(candidate, decision)).status == "running"


async def test_changed_operation_cannot_use_old_decision(tmp_path):
    repository, run, lease, clock = await setup_repository(tmp_path)
    await seed_operation(repository, run, lease, operation("read_only", "running"))
    clock.value += timedelta(seconds=31)
    candidate = await repository.expire_and_classify(lease.attempt_id)
    decision = classify_recovery(candidate.snapshot, candidate.operation,
                                 RecoveryObservation(policy_digest="policy"))
    async with aiosqlite.connect(repository.path) as db:
        await db.execute("UPDATE execution_operations SET input_digest='changed'")
        await db.commit()
    with pytest.raises(RecoveryConflict):
        await repository.apply_recovery(candidate, decision)


async def test_recovered_snapshot_cursor_matches_atomic_event(tmp_path):
    repository, run, lease, clock = await setup_repository(tmp_path)
    clock.value += timedelta(seconds=31)
    candidate = await repository.expire_and_classify(lease.attempt_id)
    decision = classify_recovery(candidate.snapshot, None, RecoveryObservation(policy_digest="policy"))
    await repository.apply_recovery(candidate, decision)
    snap = await repository.get_snapshot(run.id)
    events = await RunRepository(repository.path).list_events(run.id)
    assert snap.last_event_sequence == events[-1].sequence
    assert snap.checkpoint_revision == 2


def test_artifact_duplicate_reference_and_wrong_digest_do_not_confirm():
    op = operation("artifact")
    ref = {"artifact_id": "same", "operation_id": str(op.id), "content_digest": "output"}
    assert classify_recovery(snapshot(uuid4()), op, RecoveryObservation(
        policy_digest="policy", artifact_refs=[ref, ref])).action == "manual_review"
    assert classify_recovery(snapshot(uuid4()), op, RecoveryObservation(
        policy_digest="policy", artifact_refs=[dict(ref, content_digest="changed")])).action == "manual_review"


def test_pending_tool_without_persisted_response_is_rejected():
    snap = snapshot(uuid4()).model_copy(update={"phase": "pending_tool"})
    assert classify_recovery(snap, None, RecoveryObservation(policy_digest="policy")).action == "reject"