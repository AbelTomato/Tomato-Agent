from __future__ import annotations

from datetime import datetime
from typing import Literal

from .models import AttemptStatus, LeaseHandle, OperationStatus


class InvalidTransition(ValueError):
    pass


class StaleExecutionOwner(RuntimeError):
    pass


_ATTEMPT_TRANSITIONS: dict[AttemptStatus, set[AttemptStatus]] = {
    "running": {"succeeded", "failed", "cancelled", "expired", "released"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
    "expired": set(),
    "released": set(),
}

_OPERATION_TRANSITIONS: dict[OperationStatus, set[OperationStatus]] = {
    "prepared": {"running"},
    "running": {"succeeded", "failed", "unknown"},
    "succeeded": set(),
    "failed": set(),
    "unknown": {"succeeded", "prepared"},
}


def transition_attempt(current: AttemptStatus, target: AttemptStatus) -> AttemptStatus:
    if target not in _ATTEMPT_TRANSITIONS[current]:
        raise InvalidTransition(f"attempt cannot transition from {current} to {target}")
    return target


def transition_operation(
    current: OperationStatus,
    target: OperationStatus,
    *,
    confirmed: bool = False,
) -> OperationStatus:
    if current == "unknown" and target == "succeeded" and not confirmed:
        raise InvalidTransition("unknown operation outcome requires confirmation")
    if target not in _OPERATION_TRANSITIONS[current]:
        raise InvalidTransition(f"operation cannot transition from {current} to {target}")
    return target


def validate_lease(lease: LeaseHandle, *, now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include timezone information")
    if lease.lease_expires_at <= now:
        raise StaleExecutionOwner("lease has expired")


def normalize_digest_payload(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): normalize_digest_payload(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [normalize_digest_payload(item) for item in value]
    if isinstance(value, set):
        return [normalize_digest_payload(item) for item in sorted(value, key=repr)]
    return value