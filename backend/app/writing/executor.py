import asyncio
from uuid import UUID, uuid4

from app.writing.draft import DraftGenerator
from app.writing.execution_models import (
    DraftConfig,
    ExecutionConfig,
    GeneratedOutline,
    WritingExecutionError,
)
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
        draft_generator: DraftGenerator | None = None,
        draft_config: DraftConfig | None = None,
    ) -> None:
        self.service = service
        self.repository = repository
        self.researcher = researcher
        self.generator = generator
        self.config = config
        self.model_id = model_id
        self.draft_generator = draft_generator
        self.draft_config = draft_config or DraftConfig()

    async def execute_draft(self, task_id: UUID, *, expected_version: int) -> WritingTask:
        try:
            task = await self.service.get_task(task_id)
        except WritingNotFoundError:
            raise
        except Exception:
            raise WritingExecutionError("storage_unavailable") from None
        if not self.model_id.strip():
            raise WritingExecutionError("model_unconfigured")
        if self.draft_generator is None:
            raise WritingExecutionError("model_unconfigured")
        try:
            existing = await self.repository.get_attempt(task_id, expected_version, kind="draft")
        except Exception:
            raise WritingExecutionError("storage_unavailable") from None
        if existing is not None:
            if existing.status == "completed" and task.drafting_run_id == existing.run_id:
                return task
            if existing.status == "running":
                raise WritingConflictError("draft execution is already running")
            raise WritingConflictError("failed draft must be retried at a new version")
        if task.status != WritingStatus.DRAFTING or task.version != expected_version:
            raise WritingConflictError("draft task status or version is stale")

        attempt_id = uuid4()
        try:
            attempt = await self.repository.start_draft_attempt(
                task_id, expected_version=expected_version, config=self.draft_config,
                model_id=self.model_id, attempt_id=attempt_id,
            )
        except asyncio.CancelledError:
            raise
        except WritingConflictError:
            raise
        except Exception:
            try:
                raced = await self.repository.get_attempt(task_id, expected_version, kind="draft")
            except Exception:
                raise WritingExecutionError("storage_unavailable") from None
            if raced is not None:
                raise WritingConflictError("draft attempt already exists") from None
            raise WritingExecutionError("storage_unavailable") from None

        try:
            async with asyncio.timeout(self.draft_config.timeout_seconds):
                await self.repository.set_phase(attempt.attempt_id, "generation")
                outline = GeneratedOutline.model_validate(task.outline, strict=True)
                draft = await self.draft_generator.generate(
                    task.topic, outline, task.citations, config=self.draft_config,
                )
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
            return await self._record_failure(attempt.attempt_id, "provider_failed")

        try:
            return await self.repository.complete_draft_attempt(attempt.attempt_id, draft=draft)
        except WritingConflictError:
            raise
        except Exception:
            resolved = await self._read_publication(attempt.attempt_id)
            if resolved is not None:
                current, stored, _ = resolved
                if stored.status == "completed" and current.drafting_run_id == stored.run_id:
                    return current
                if stored.status == "conflicted":
                    raise WritingConflictError("draft result became stale") from None
            raise WritingExecutionError("storage_unavailable") from None

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
                await self.repository.record_retrieval_result(
                    attempt.attempt_id,
                    retrieval_mode=bundle.retrieval_mode,
                    fallback_reason=bundle.retrieval_fallback_reason,
                )
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
        cleanup = asyncio.create_task(
            self.repository.fail_attempt(attempt_id, error_code="cancelled")
        )
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # Cancellation may arrive again while the failure transaction is
                # committing. Keep waiting so a running attempt is not orphaned.
                continue
            except Exception:
                return
        try:
            cleanup.result()
        except BaseException:
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