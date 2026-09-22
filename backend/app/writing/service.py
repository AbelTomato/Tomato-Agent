import os
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from app.knowledge.service import CitationSnapshot

from .models import WritingStatus, WritingTask
from .repository import WritingRepository


class WritingNotFoundError(ValueError):
    pass


class WritingConflictError(ValueError):
    pass


class WritingService:
    def __init__(
        self,
        repository: WritingRepository,
        draft_directory: Path = Path("data/drafts"),
    ):
        self.repository = repository
        self.draft_directory = draft_directory

    async def create_task(self, session_id: UUID, topic: str) -> WritingTask:
        if not topic.strip():
            raise ValueError("topic cannot be empty")
        return await self.repository.create_task(session_id, topic.strip())

    async def get_task(self, task_id: UUID) -> WritingTask:
        task = await self.repository.get_task(task_id)
        if task is None:
            raise WritingNotFoundError(f"Writing task not found: {task_id}")
        return task

    async def publish_outline(
        self,
        task_id: UUID,
        outline: dict[str, Any],
        citations: list[CitationSnapshot],
        research_run_id: UUID | None = None,
    ) -> WritingTask:
        task = await self.get_task(task_id)
        if task.status != WritingStatus.RESEARCHING:
            raise WritingConflictError("writing task is not researching")
        try:
            return await self.repository.transition(
                task_id,
                expected_status=WritingStatus.RESEARCHING,
                expected_version=task.version,
                status=WritingStatus.AWAITING_OUTLINE_CONFIRMATION,
                outline=outline,
                citations=citations,
                research_run_id=research_run_id,
            )
        except RuntimeError as exc:
            raise WritingConflictError("writing task changed concurrently") from exc

    async def confirm_outline(
        self,
        task_id: UUID,
        *,
        expected_version: int,
        outline: dict[str, Any],
    ) -> WritingTask:
        task = await self.get_task(task_id)
        if (
            task.status != WritingStatus.AWAITING_OUTLINE_CONFIRMATION
            or task.version != expected_version
        ):
            raise WritingConflictError("outline confirmation is stale or invalid")
        try:
            return await self.repository.transition(
                task_id,
                expected_status=WritingStatus.AWAITING_OUTLINE_CONFIRMATION,
                expected_version=expected_version,
                status=WritingStatus.DRAFTING,
                outline=outline,
                citations=task.citations,
                research_run_id=task.research_run_id,
            )
        except RuntimeError as exc:
            raise WritingConflictError("outline confirmation changed concurrently") from exc

    async def publish_draft(
        self,
        task_id: UUID,
        *,
        expected_version: int,
        draft: str,
        drafting_run_id: UUID,
    ) -> WritingTask:
        if not draft.strip():
            raise ValueError("draft cannot be empty")
        task = await self.get_task(task_id)
        if (
            task.status != WritingStatus.DRAFTING
            or task.version != expected_version
        ):
            raise WritingConflictError("draft publication is stale or invalid")
        try:
            return await self.repository.transition(
                task_id,
                expected_status=WritingStatus.DRAFTING,
                expected_version=expected_version,
                status=WritingStatus.AWAITING_SAVE_CONFIRMATION,
                outline=task.outline,
                citations=task.citations,
                research_run_id=task.research_run_id,
                draft=draft,
                drafting_run_id=drafting_run_id,
            )
        except RuntimeError as exc:
            raise WritingConflictError("draft publication changed concurrently") from exc

    async def save(
        self,
        task_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> WritingTask:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key cannot be empty")
        task = await self.get_task(task_id)
        if task.status == WritingStatus.SAVED and task.saved_path:
            if await self.repository.has_save_key(task_id, idempotency_key):
                return task
            raise WritingConflictError("writing task is already saved")
        if (
            task.status != WritingStatus.AWAITING_SAVE_CONFIRMATION
            or task.version != expected_version
            or not task.draft
        ):
            raise WritingConflictError("draft save is stale or invalid")

        self.draft_directory.mkdir(parents=True, exist_ok=True)
        target = self.draft_directory / f"{task.task_id}-v{task.version}.md"
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{task.task_id}-",
            suffix=".tmp",
            dir=self.draft_directory,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as temporary:
                temporary.write(task.draft)
                temporary.flush()
                os.fsync(temporary.fileno())
            if target.exists() and target.read_text(encoding="utf-8") != task.draft:
                raise WritingConflictError("draft path already contains different content")
            os.replace(temporary_name, target)
        except Exception:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
            raise

        try:
            return await self.repository.save_task(
                task_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                saved_path=str(target),
            )
        except RuntimeError as exc:
            raise WritingConflictError("draft save changed concurrently") from exc

    async def fail(
        self,
        task_id: UUID,
        *,
        expected_version: int,
        failed_stage: str,
    ) -> WritingTask:
        if failed_stage not in {
            WritingStatus.RESEARCHING.value,
            WritingStatus.DRAFTING.value,
            "saving",
        }:
            raise ValueError("unsupported failed stage")
        try:
            return await self.repository.mark_failed(
                task_id,
                expected_version=expected_version,
                failed_stage=failed_stage,
            )
        except RuntimeError as exc:
            raise WritingConflictError("writing task failure transition is stale") from exc

    async def retry(self, task_id: UUID, *, expected_version: int) -> WritingTask:
        task = await self.get_task(task_id)
        if task.status != WritingStatus.FAILED or task.failed_stage not in {
            WritingStatus.RESEARCHING.value,
            WritingStatus.DRAFTING.value,
        }:
            raise WritingConflictError("only read-only generation failures can be retried")
        try:
            return await self.repository.retry_generation(
                task_id,
                expected_version=expected_version,
                status=WritingStatus(task.failed_stage),
            )
        except RuntimeError as exc:
            raise WritingConflictError("writing task retry is stale") from exc
