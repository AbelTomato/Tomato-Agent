from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.execution.models import LeaseHandle
from app.execution.state_machine import (
    InvalidTransition,
    StaleExecutionOwner,
    transition_attempt,
    transition_operation,
    validate_lease,
)


def test_terminal_attempt_cannot_transition():
    with pytest.raises(InvalidTransition):
        transition_attempt("succeeded", "failed")


def test_unknown_write_requires_confirmation():
    with pytest.raises(InvalidTransition):
        transition_operation("unknown", "succeeded", confirmed=False)

    assert transition_operation("unknown", "succeeded", confirmed=True) == "succeeded"


def test_expired_lease_cannot_renew():
    now = datetime.now(timezone.utc)
    lease = LeaseHandle(
        run_id=uuid4(),
        attempt_id=uuid4(),
        owner_id="owner",
        fencing_token=1,
        lease_expires_at=now - timedelta(seconds=1),
    )

    with pytest.raises(StaleExecutionOwner):
        validate_lease(lease, now=now)