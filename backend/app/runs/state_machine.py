from .models import RunStatus


_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    "queued": {"running", "cancelled", "timed_out", "failed"},
    "running": {"waiting", "completed", "failed", "cancelled", "timed_out"},
    "waiting": {"running", "completed", "failed", "cancelled", "timed_out"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
    "timed_out": set(),
}


def transition_run(
    current: RunStatus, target: RunStatus, *, resume_confirmed: bool = False
) -> RunStatus:
    if current not in _TRANSITIONS or target not in _TRANSITIONS:
        raise ValueError(f"Unknown run status: {current} -> {target}")
    if current == "waiting" and target == "queued" and resume_confirmed:
        return target
    if current == target or target in _TRANSITIONS[current]:
        return target
    raise ValueError(f"Invalid run status transition: {current} -> {target}")