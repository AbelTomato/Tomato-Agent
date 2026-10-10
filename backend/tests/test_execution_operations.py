from datetime import timedelta
from uuid import uuid4

import pytest

from app.agent.models import LLMResponse
from app.execution.models import OperationInput, StepOutcome
from app.execution.state_machine import StaleExecutionOwner
from test_execution_recovery import setup_repository, snapshot


async def test_atomic_operation_lifecycle_and_input_conflict(tmp_path):
    repo, run, lease, clock = await setup_repository(tmp_path)
    snap = snapshot(run.id)
    op = OperationInput(id=uuid4(), logical_index=0, kind="tool", tool_name="read",
                        input_payload={"path": "x"}, recovery_class="read_only")
    stored = await repo.prepare_operation(lease, op, snap)
    assert stored.status == "prepared"
    with pytest.raises(RuntimeError, match="operation_input_conflict"):
        await repo.prepare_operation(lease, op.model_copy(update={"input_payload": {"path": "y"}}), snap)
    step = await repo.start_step(lease, stored.id, snap)
    saved = await repo.commit_step(lease, step.id, StepOutcome(success=True, result_payload={"data": "ok"}), snap)
    assert saved.last_event_sequence > 0
    assert (await repo.get_operation(stored.id)).status == "succeeded"
    with pytest.raises(RuntimeError, match="invalid_transition"):
        await repo.start_step(lease, stored.id, saved)
    clock.value += timedelta(seconds=31)
    with pytest.raises(StaleExecutionOwner):
        await repo.commit_model_response(lease, LLMResponse(kind="final", content="done"), saved)


async def test_model_operation_and_snapshot_commit_roll_back_together(tmp_path, monkeypatch):
    repo, run, lease, _ = await setup_repository(tmp_path)
    before = await repo.get_snapshot(run.id)
    op = OperationInput(id=uuid4(), logical_index=0, kind="tool", tool_name="read",
                        input_payload={}, recovery_class="read_only")
    async def fail(db):
        raise RuntimeError("injected")
    monkeypatch.setattr(repo, "_commit", fail)
    with pytest.raises(RuntimeError, match="injected"):
        await repo.commit_model_response(lease, LLMResponse(kind="tool_call"), before, op)
    assert await repo.get_snapshot(run.id) == before
    with pytest.raises(KeyError):
        await repo.get_operation(op.id)


async def test_same_arguments_cannot_change_operation_tool_or_recovery_class(tmp_path):
    repo, run, lease, _ = await setup_repository(tmp_path)
    op = OperationInput(id=uuid4(), logical_index=0, kind="tool", tool_name="read",
                        input_payload={}, recovery_class="read_only")
    await repo.prepare_operation(lease, op, snapshot(run.id))
    with pytest.raises(RuntimeError, match="operation_input_conflict"):
        await repo.prepare_operation(lease,
            op.model_copy(update={"tool_name": "write", "recovery_class": "file_write"}), snapshot(run.id))


def test_recoverable_arguments_reject_sensitive_fields():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        OperationInput(id=uuid4(), logical_index=0, kind="tool", tool_name="read",
                       input_payload={"nested": {"api_key": "secret"}}, recovery_class="read_only")