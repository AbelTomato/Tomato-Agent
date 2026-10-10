from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import logging
from uuid import uuid4
from uuid import UUID

from .ports import LeaseExecutionPort, WorkerRepository
from .recovery import RecoveryObservation, classify_recovery
from .state_machine import StaleExecutionOwner

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryResult:
    run_id: UUID
    status: str
    action: str
    reason_code: str


class ExecutionWorker:
    """Single-process coordinator for lease-bound background execution."""

    def __init__(self, repository: WorkerRepository, executor_factory, *,
                 poll_interval_seconds: float = 1.0, max_concurrency: int = 1,
                 lease_seconds: int = 30, heartbeat_interval_seconds: float = 10.0,
                 recovery_observation_factory=None, recovery_scan_limit: int = 100):
        if poll_interval_seconds <= 0 or max_concurrency != 1:
            raise ValueError("poll interval must be positive and max_concurrency must be 1")
        if lease_seconds <= 0 or heartbeat_interval_seconds <= 0 or recovery_scan_limit <= 0:
            raise ValueError("lease, heartbeat and recovery limits must be positive")
        self.repository = repository
        self.executor_factory = executor_factory
        self.poll_interval_seconds = poll_interval_seconds
        self.max_concurrency = max_concurrency
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.recovery_observation_factory = recovery_observation_factory
        self.recovery_scan_limit = recovery_scan_limit
        self._owner_id = f"worker-{uuid4()}"
        self._active: dict[asyncio.Task, object] = {}
        self._loop_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()
        self._stopped = False

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._loop_task is not None and not self._loop_task.done():
                return
            self._stop_event = asyncio.Event()
            self._stopped = False
            self._loop_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            loop_task = self._loop_task
            self._stopped = True
            if loop_task is None:
                tasks = list(self._active)
            else:
                assert self._stop_event is not None
                self._stop_event.set()
                tasks = list(self._active)
        if loop_task is not None:
            await loop_task
        tasks = list(self._active)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lifecycle_lock:
            if self._loop_task is loop_task:
                self._loop_task = None
                self._stop_event = None

    async def _run_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            while len(self._active) < self.max_concurrency and not self._stop_event.is_set():
                try:
                    claimed = await self.run_once()
                except Exception:
                    logger.error("Worker execution iteration failed")
                    break
                if not claimed:
                    break
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> bool:
        async with self._run_lock:
            if self._stopped:
                return False
            if len(self._active) >= self.max_concurrency:
                return False
            claimed = await self.repository.claim_next(self._owner_id, lease_seconds=self.lease_seconds)
            if claimed is None:
                return False
            run, lease = claimed
            task = asyncio.create_task(self._execute(run, lease))
            self._active[task] = run.id
        try:
            await task
            return True
        finally:
            self._active.pop(task, None)

    async def _execute(self, run, lease) -> None:
        port = LeaseExecutionPort(self.repository, lease, run.workspace_id)
        execution = asyncio.create_task(self.executor_factory(run, lease, port))
        lost = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(port, execution, lost))
        try:
            await execution
        except asyncio.CancelledError:
            if lost.is_set():
                return
            raise
        except StaleExecutionOwner:
            return
        except Exception:
            logger.error("Worker executor failed for run %s", run.id)
            try:
                await port.finish(
                    "failed", event_type="execution.failed",
                    event_payload={"code": "worker_executor_failed"},
                    error={"code": "worker_executor_failed"},
                )
            except StaleExecutionOwner:
                pass
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat(self, port, execution, lost) -> None:
        try:
            while True:
                await asyncio.sleep(self.heartbeat_interval_seconds)
                await port.heartbeat(lease_seconds=self.lease_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            lost.set()
            execution.cancel()

    async def recover_on_startup(self, *, observation_factory=None) -> list[RecoveryResult]:
        factory = observation_factory or self.recovery_observation_factory
        expired = await self.repository.scan_expired(self.recovery_scan_limit)
        if expired and factory is None:
            raise ValueError("current recovery observation factory is required")
        results = []
        for attempt in expired:
            candidate = await self.repository.get_recovery_candidate(attempt.run_id)
            observation = factory(candidate)
            if inspect.isawaitable(observation):
                observation = await observation
            if not isinstance(observation, RecoveryObservation):
                observation = RecoveryObservation.model_validate(observation)
            candidate = await self.repository.expire_and_classify(attempt.id)
            decision = classify_recovery(candidate.snapshot, candidate.operation, observation)
            run = await self.repository.apply_recovery(candidate, decision)
            results.append(RecoveryResult(run_id=run.id, status=run.status,
                                          action=decision.action, reason_code=decision.reason_code))
        return results