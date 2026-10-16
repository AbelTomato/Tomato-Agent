"""Persistent scenario state for the opt-in Runtime Provider acceptance harness."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal


ScenarioStatus = Literal[
    "pending", "running", "passed", "failed", "timed_out", "cancelled", "interrupted"
]
TERMINAL_STATUSES = {"passed", "failed", "timed_out", "cancelled", "interrupted"}
_TRANSITIONS = {
    "pending": {"running", "failed", "cancelled"},
    "running": TERMINAL_STATUSES - {"interrupted"},
}


class AcceptanceStateError(RuntimeError):
    """Raised when a scenario state or budget operation violates its contract."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class ScenarioRecord:
    acceptance_id: str
    scenario_id: str
    run_id: str | None = None
    status: ScenarioStatus = "pending"
    timeout_seconds: float = 0
    request_budget: int = 0
    request_count: int = 0
    expected_terminal: str | None = None
    expected_events: list[str] = field(default_factory=list)
    expected_operations: list[str] = field(default_factory=list)
    actual_observations: dict[str, Any] = field(default_factory=dict)
    failed_phase: str | None = None
    error_category: str | None = None
    diagnostic_files: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    started_at: str | None = None
    ended_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ScenarioStore:
    """Atomically persist scenario summaries outside business databases."""

    def __init__(self, path: str | Path, acceptance_id: str | None = None):
        self.path = Path(path)
        self.acceptance_id = acceptance_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                scenarios = data["scenarios"]
                if not isinstance(scenarios, dict):
                    raise ValueError("scenarios must be an object")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise AcceptanceStateError("invalid_acceptance_summary") from exc
            self._scenarios = {
                key: ScenarioRecord(**value) for key, value in scenarios.items()
            }
            self.acceptance_id = self.acceptance_id or data.get("acceptance_id")
        else:
            self._scenarios: dict[str, ScenarioRecord] = {}
        self._write()

    def add(self, record: ScenarioRecord) -> None:
        if record.scenario_id in self._scenarios:
            raise AcceptanceStateError("scenario_already_exists")
        if self.acceptance_id is not None and record.acceptance_id != self.acceptance_id:
            raise AcceptanceStateError("acceptance_id_mismatch")
        self.acceptance_id = self.acceptance_id or record.acceptance_id
        self._scenarios[record.scenario_id] = record
        self._write()

    def get(self, scenario_id: str) -> dict[str, Any]:
        try:
            return self._scenarios[scenario_id].to_dict()
        except KeyError as exc:
            raise AcceptanceStateError("unknown_scenario") from exc

    def update(self, scenario_id: str, **values: Any) -> None:
        record = self._record(scenario_id)
        if record.status in TERMINAL_STATUSES:
            raise AcceptanceStateError("terminal_scenario_is_immutable")
        allowed = {"run_id", "actual_observations", "diagnostic_files"}
        if values.keys() - allowed:
            raise AcceptanceStateError("unsupported_scenario_update")
        for key, value in values.items():
            current = getattr(record, key)
            if isinstance(current, dict) and isinstance(value, dict):
                current.update(value)
            else:
                setattr(record, key, value)
        self._touch(record)
        self._write()

    def transition(self, scenario_id: str, status: ScenarioStatus, **values: Any) -> None:
        record = self._record(scenario_id)
        if status not in _TRANSITIONS.get(record.status, set()):
            raise AcceptanceStateError(f"invalid_transition:{record.status}->{status}")
        allowed = {"run_id", "actual_observations", "failed_phase", "error_category", "diagnostic_files"}
        if values.keys() - allowed:
            raise AcceptanceStateError("unsupported_transition_update")
        for key, value in values.items():
            current = getattr(record, key)
            if isinstance(current, dict) and isinstance(value, dict):
                current.update(value)
            else:
                setattr(record, key, value)
        now = _now()
        record.status = status
        if status == "running":
            record.started_at = now
        if status in TERMINAL_STATUSES:
            record.ended_at = now
        record.updated_at = now
        self._write()

    def consume_request(self, scenario_id: str) -> int:
        record = self._record(scenario_id)
        if record.status != "running":
            raise AcceptanceStateError("scenario_not_running")
        if record.request_count >= record.request_budget:
            raise AcceptanceStateError("request_budget_exhausted")
        record.request_count += 1
        self._touch(record)
        self._write()
        return record.request_count

    def recover_running(self) -> list[str]:
        interrupted = []
        for record in self._scenarios.values():
            if record.status == "running":
                record.status = "interrupted"
                record.ended_at = _now()
                self._touch(record)
                interrupted.append(record.scenario_id)
        if interrupted:
            self._write()
        return interrupted

    def _record(self, scenario_id: str) -> ScenarioRecord:
        try:
            return self._scenarios[scenario_id]
        except KeyError as exc:
            raise AcceptanceStateError("unknown_scenario") from exc

    @staticmethod
    def _touch(record: ScenarioRecord) -> None:
        record.updated_at = _now()

    def _write(self) -> None:
        data = {
            "mode": "real_provider",
            "acceptance_id": self.acceptance_id,
            "updated_at": _now(),
            "scenarios": {key: value.to_dict() for key, value in self._scenarios.items()},
        }
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
