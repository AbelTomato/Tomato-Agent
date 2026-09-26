import asyncio
from uuid import UUID, uuid4

from app.writing.execution_models import ExecutionConfig, WritingExecutionError
from app.writing.execution_repository import WritingExecutionRepository
from app.writing.models import WritingStatus, WritingTask
from app.writing.outline import OutlineGenerator, validate_outline
from app.writing.research import WritingResearcher
from app.writing.service import (
    WritingConflictError,
    WritingNotFoundError,
    WritingService,
)


class WritingTaskExecutor:
    def __init__(
        self,
        service: WritingService,
        repository: WritingExecutionRepository,
        researcher: WritingResearcher,
        generator: OutlineGenerator,
        *,
        config: ExecutionConfig,
        model_id: str,
    ) -> None:
        self.service = service
        self.repository = repository
        self.researcher = researcher
        self.generator = generator
        self.config = config
        self.model_id = model_id

    async def execute_research(self, task_id: UUID, *, expected_version: int) -> WritingTask:
        try:
            task = await self.service.get_task(task_id)
        except WritingNotFoundError:
            raise
        except Exception:
            raise WritingExecutionError("storage_unavailable") from None
        if not self.model_id.strip():
            raise WritingExecutionError("model_unconfigured")
        try:
            existing = await self.repository.get_attempt(task_id, expected_version)
        except Exception:
            raise WritingExecutionError("storage_unavailable") from None
        if existing is not None:
            if existing.status == "completed" and task.research_run_id == existing.run_id:
                return task
            if existing.status == "running":
                raise WritingConflictError("research execution is already running")
            if existing.status == "failed":
                raise WritingConflictError("failed research must be retried at a new version")
            raise WritingConflictError("research attempt is no longer current")

        if task.status != WritingStatus.RESEARCHING or task.version != expected_version:
            raise WritingConflictError("research task status or version is stale")

        attempt_id = uuid4()
        try:
            attempt = await self.repository.start_attempt(
                task_id,
                expected_version=expected_version,
                config=self.config,
                model_id=self.model_id,
                attempt_id=attempt_id,
            )
        except asyncio.CancelledError:
            try:
                try:
                    started = await self.repository.get_attempt_by_id(attempt_id)
                    if started is not None and started.status == "running":
                        await self._record_cancelled(started.attempt_id)
                except Exception:
                    pass
            finally:
                raise
        except WritingConflictError:
            raise
        except Exception:
            try:
                raced = await self.repository.get_attempt(task_id, expected_version)
            except Exception:
                raise WritingExecutionError("storage_unavailable") from None
            if raced is not None:
                raise WritingConflictError("research attempt already exists") from None
            raise WritingExecutionError("storage_unavailable") from None

        phase = "retrieval"
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                await self.repository.set_phase(attempt.attempt_id, "retrieval")
                bundle = await self.researcher.collect(task.topic, config=self.config)
                phase = "generation"
                await self.repository.set_phase(attempt.attempt_id, "generation")
                raw, citations = await self.generator.generate(
                    task.topic, bundle, config=self.config,
                )
                phase = "validation"
                await self.repository.set_phase(attempt.attempt_id, "validation")
                outline = validate_outline(
                    raw, citations, max_response_chars=self.config.max_response_chars,
                )
                phase = "publication"
                await self.repository.set_phase(attempt.attempt_id, "publication")
        except TimeoutError:
            return await self._record_failure(attempt.attempt_id, "deadline_exceeded")
        except asyncio.CancelledError:
            await self._record_cancelled(attempt.attempt_id)
            raise
        except WritingExecutionError as error:
            return await self._record_failure(attempt.attempt_id, error.code)
        except WritingConflictError:
            raise
        except Exception:
            code = "retrieval_failed" if phase == "retrieval" else "provider_failed"
            if phase == "validation":
                code = "invalid_model_response"
            elif phase == "publication":
                code = "storage_unavailable"
            return await self._record_failure(attempt.attempt_id, code)

        try:
            return await self.repository.complete_attempt(
                attempt.attempt_id, outline=outline, citations=citations,
            )
        except asyncio.CancelledError:
            await self._resolve_publication(attempt.attempt_id, outline=outline, citations=citations)
            raise
        except Exception:
            resolved = await self._read_publication(attempt.attempt_id)
            if resolved is not None:
                current, stored, run = resolved
                if stored.status == "completed" and current.research_run_id == stored.run_id:
                    return current
                if stored.status == "conflicted":
                    raise WritingConflictError("research result became stale") from None
                if stored.status == "running":
                    return await self._record_failure(attempt.attempt_id, "publication_failed")
            raise WritingExecutionError("storage_unavailable") from None

    async def _record_failure(self, attempt_id: UUID, code: str) -> WritingTask:
        try:
            await self.repository.fail_attempt(attempt_id, error_code=code)
        except WritingConflictError:
            raise
        except Exception:
            raise WritingExecutionError("storage_unavailable") from None
        raise WritingExecutionError(code)

    async def _record_cancelled(self, attempt_id: UUID) -> None:
        try:
            await self.repository.fail_attempt(attempt_id, error_code="cancelled")
        except Exception:
            pass

    async def _read_publication(self, attempt_id: UUID):
        try:
            attempt = await self.repository.get_attempt_by_id(attempt_id)
            if attempt is None:
                return None
            task = await self.service.get_task(attempt.task_id)
            run = await self.repository.get_run_for_attempt(attempt_id)
            if run is None:
                return None
            state = run["state"]
            if (
                run["status"] != "completed"
                or
                run["id"] != str(attempt.run_id)
                or state.get("task_id") != str(task.task_id)
                or state.get("attempt_id") != str(attempt.attempt_id)
            ):
                return None
            return task, attempt, run
        except Exception:
            return None

    async def _resolve_publication(self, attempt_id: UUID, *, outline, citations) -> None:
        resolved = await self._read_publication(attempt_id)
        if resolved is not None and resolved[1].status == "running":
            try:
                await self.repository.fail_attempt(attempt_id, error_code="publication_failed")
            except Exception:
                pass