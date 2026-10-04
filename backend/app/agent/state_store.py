from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from .models import CheckpointRecord, Event, RunRecord, SessionRecord


@runtime_checkable
class RunStateStore(Protocol):
    """Runtime 所需的异步 run/session/event/checkpoint 存储能力。"""

    async def create_run(self, session_id: UUID) -> UUID: ...

    async def get_run(
        self, run_id: UUID, session_id: UUID | None = None
    ) -> RunRecord | None: ...

    async def update_run(
        self,
        run_id: UUID,
        status: str,
        state: dict[str, Any] | None = None,
        loop_count: int = 0,
    ) -> None: ...

    async def get_session(self, session_id: UUID) -> SessionRecord | None: ...

    async def list_completed_turn_events(self, session_id: UUID) -> list[Event]: ...

    async def append_event(
        self,
        session_id: UUID,
        run_id: UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> int: ...

    async def list_events(
        self, run_id: UUID, after_sequence: int = 0
    ) -> list[Event]: ...

    async def save_checkpoint(
        self, run_id: UUID, sequence: int, state: dict[str, Any]
    ) -> None: ...

    async def get_checkpoint(self, run_id: UUID) -> CheckpointRecord | None: ...