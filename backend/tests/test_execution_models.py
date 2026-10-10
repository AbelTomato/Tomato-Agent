from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.execution.models import (
    AttemptRecord,
    BudgetState,
    ExecutionSnapshot,
    LeaseHandle,
    PROTOCOL_VERSION,
)
from app.execution.ports import execution_migration


def test_invalid_snapshot_is_rejected():
    with pytest.raises(ValidationError):
        ExecutionSnapshot(
            run_id=uuid4(),
            protocol_version=PROTOCOL_VERSION,
            schema_version=1,
            checkpoint_revision=1,
            last_event_sequence=0,
            phase="not-a-phase",
            logical_index=0,
            messages=[],
            harness_state={},
            pending_operation_id=None,
            pending_response=None,
            budget_limits={},
            reserved_model_calls=0,
            reserved_tool_calls=0,
            loop_count=0,
            elapsed_upper_bound_seconds=0.0,
            policy_digest="digest",
            workspace_id="workspace",
            test_results=[],
            artifact_ids=[],
        )


def test_budget_reservation_is_not_refunded():
    budget = BudgetState(max_model_calls=2, max_tool_calls=1)
    reserved = budget.reserve_model_call()

    assert reserved.reserved_model_calls == 1
    assert reserved.record_failed_model_call().reserved_model_calls == 1


def test_lease_handle_requires_timezone_aware_expiry():
    with pytest.raises(ValidationError):
        LeaseHandle(
            run_id=uuid4(),
            attempt_id=uuid4(),
            owner_id="owner",
            fencing_token=1,
            lease_expires_at=datetime.now(),
        )


def test_attempt_record_uses_protocol_status_values():
    attempt = AttemptRecord(
        id=uuid4(),
        run_id=uuid4(),
        attempt_no=1,
        owner_id="owner",
        fencing_token=1,
        status="running",
        lease_expires_at=datetime.now(timezone.utc),
        heartbeat_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
    )

    assert attempt.status == "running"


def test_execution_migration_is_versioned_and_pure():
    statements = execution_migration()

    assert len(statements) == 4
    assert "CREATE TABLE IF NOT EXISTS execution_run_heads" in statements[0]
    assert "CREATE TABLE IF NOT EXISTS execution_attempts" in statements[1]
    assert "IF NOT EXISTS" in statements[2]
    with pytest.raises(ValueError):
        execution_migration(version=2)