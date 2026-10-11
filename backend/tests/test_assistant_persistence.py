import json
from datetime import timedelta
from uuid import uuid4

import pytest

from app.execution import models
from app.execution.recovery import RecoveryObservation, classify_recovery
from app.execution.state_machine import StaleExecutionOwner
from test_execution_recovery import setup_repository, snapshot, operation


def assistant_snapshot(run_id, **changes):
    raw = snapshot(run_id).model_dump(mode="json")
    raw.update(snapshot_type="assistant", workspace_id=None, session_id=str(uuid4()),
               config_digest="config", reserved_model_calls=0, reserved_tool_calls=0,
               elapsed_upper_bound_seconds=0.0,
               budget_limits={"max_model_calls": 2, "max_execution_seconds": 20.0})
    raw.update(changes)
    cls = getattr(models, "AssistantExecutionSnapshot", None)
    assert cls is not None, "assistant snapshot contract is missing"
    return cls.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("discriminator", [None, "code", "stage", "assistant"])
def test_four_snapshot_forms(discriminator):
    run_id = uuid4()
    if discriminator == "assistant":
        snap = assistant_snapshot(run_id)
        raw = snap.model_dump(mode="json")
    elif discriminator == "stage":
        raw = dict(snapshot_type="stage", run_id=str(run_id), checkpoint_revision=1,
                   last_event_sequence=0, kind="chat", phase="ready", stage="chat",
                   input_version=0, policy_digest="policy", config_digest="config")
    else:
        raw = snapshot(run_id).model_dump(mode="json")
        if discriminator is None:
            raw.pop("snapshot_type")
    parsed = models.parse_execution_snapshot(json.dumps(raw))
    assert str(parsed.run_id) == str(run_id)
    if discriminator == "assistant":
        assert parsed.workspace_id is None
        for field in ("schema_version", "protocol_version"):
            with pytest.raises(ValueError):
                models.parse_execution_snapshot(json.dumps({**raw, field: 2}))


async def test_persistence_migration_and_stale_owner(tmp_path):
    repo, run, lease, clock = await setup_repository(tmp_path)
    snap = assistant_snapshot(run.id)
    await repo.save_snapshot(lease, snap)
    await repo.init()
    await repo.init()
    assert isinstance(await repo.get_snapshot(run.id), models.AssistantExecutionSnapshot)
    clock.value += timedelta(seconds=31)
    with pytest.raises(StaleExecutionOwner):
        await repo.save_snapshot(lease, snap)


@pytest.mark.parametrize("kind,expected", [("read_only", "retry"),
                                           ("file_write", "manual_review"),
                                           ("sandbox", "manual_review")])
def test_assistant_uses_public_operation_recovery(kind, expected):
    snap = assistant_snapshot(uuid4())
    assert classify_recovery(snap, operation(kind), RecoveryObservation(
        policy_digest="policy")).action == expected


def test_published_domain_result_requires_matching_identity_version_and_key():
    snap = assistant_snapshot(uuid4())
    op = operation().model_copy(update={"recovery_class": "domain_publish",
        "input_payload": {"domain_id": "article", "version": 2, "idempotency_key": "save-1"}})
    from app.execution.ports import canonical_digest
    op = op.model_copy(update={"input_digest": canonical_digest(op.input_payload)})
    observation = RecoveryObservation.model_validate({"policy_digest": "policy",
        "domain_publications": [{"domain_id": "article", "version": 2,
            "idempotency_key": "save-1", "result_payload": {"outline": "published"}}]})
    decision = classify_recovery(snap, op, observation)
    assert decision.action == "reconcile"
    assert decision.result_payload == {"outline": "published"}
    mismatch = observation.model_copy(update={"domain_publications": []})
    assert classify_recovery(snap, op, mismatch).action == "manual_review"


async def test_independent_process_hard_exit_keeps_model_reservation(tmp_path):
    import asyncio
    import os
    import sys
    from pathlib import Path

    repo, run, lease, clock = await setup_repository(tmp_path)
    await repo.save_snapshot(lease, assistant_snapshot(run.id))
    backend = Path(__file__).parents[1]
    script = """
import asyncio, os, sys
from uuid import UUID
from app.execution.repository import ExecutionRepository
from test_execution_recovery import Clock
async def main():
    repo = ExecutionRepository(sys.argv[1], clock=Clock())
    lease = await repo.latest_attempt(UUID(sys.argv[2]))
    from app.execution.models import LeaseHandle
    handle = LeaseHandle(run_id=lease.run_id, attempt_id=lease.id, owner_id=lease.owner_id,
        fencing_token=lease.fencing_token, lease_expires_at=lease.lease_expires_at)
    await repo.reserve_model_call(handle, scope='top', step_id='hard-exit', operation_id=None,
        phase='decision', attempt_id='top', input_digest='input', config_digest='config', timeout_seconds=4)
    os._exit(73)
asyncio.run(main())
"""
    child = await asyncio.create_subprocess_exec(sys.executable, "-c", script,
        str(repo.path), str(run.id), cwd=backend,
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(backend), str(backend / "tests")])},
        stderr=asyncio.subprocess.PIPE)
    try:
        _, stderr = await asyncio.wait_for(child.communicate(), 10)
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    assert child.returncode == 73, stderr.decode()
    await repo.init()
    assert (await repo.get_snapshot(run.id)).reserved_model_calls == 1
    clock.value += timedelta(seconds=31)
    await repo.expire_and_classify(lease.attempt_id)
    assert (await repo.get_model_calls(run.id))[0]["status"] == "unknown"
    assert (await repo.get_snapshot(run.id)).elapsed_upper_bound_seconds >= 31