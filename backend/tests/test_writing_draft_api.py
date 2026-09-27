from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from app import main
from app.writing.execution_models import DraftConfig, WritingExecutionError
from app.writing.execution_repository import WritingExecutionRepository
from app.writing.models import WritingStatus
from app.writing.repository import WritingRepository
from app.writing.service import WritingConflictError, WritingService
from app.sessions.repository import SessionRepository
from app.knowledge.service import CitationSnapshot


class FakeDraftExecutor:
    def __init__(self, service: WritingService, *, enforce_status: bool = False):
        self.service = service
        self.enforce_status = enforce_status
        self.calls: list[tuple[UUID, int]] = []
        self.error: WritingExecutionError | None = None

    async def execute_draft(self, task_id: UUID, *, expected_version: int):
        self.calls.append((task_id, expected_version))
        if self.error is not None:
            raise self.error
        task = await self.service.get_task(task_id)
        if self.enforce_status and task.status != WritingStatus.DRAFTING:
            raise WritingConflictError("task is not ready for drafting")
        return task


async def make_api_app(monkeypatch, tmp_path: Path, *, enforce_draft_status: bool = False):
    path = tmp_path / "writing-draft-api.db"
    sessions = SessionRepository(path)
    await sessions.init()
    writing = WritingRepository(path)
    await writing.init()
    service = WritingService(writing, draft_directory=tmp_path / "drafts")
    execution = WritingExecutionRepository(path)
    await execution.init()
    executor = FakeDraftExecutor(service, enforce_status=enforce_draft_status)
    monkeypatch.setattr(main.app.state, "writing_service", service)
    monkeypatch.setattr(main.app.state, "writing_executor", executor)
    monkeypatch.setattr(main.app.state, "writing_execution_repository", execution)
    return sessions, service, execution, executor


@pytest.mark.asyncio
async def test_draft_api_requires_strict_version_and_uses_injected_executor(monkeypatch, tmp_path):
    sessions, service, _, executor = await make_api_app(monkeypatch, tmp_path)
    task = await service.create_task(await sessions.create_session(), "Redis")
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        rejected = await client.post(
            f"/api/writing-tasks/{task.task_id}/draft",
            json={"version": task.version, "draft": "client-controlled", "path": "/tmp/x"},
        )
        accepted = await client.post(
            f"/api/writing-tasks/{task.task_id}/draft",
            json={"version": task.version},
        )

    assert rejected.status_code == 422
    assert accepted.status_code == 200
    assert executor.calls == [(task.task_id, task.version)]


@pytest.mark.asyncio
async def test_draft_api_maps_execution_errors(monkeypatch, tmp_path):
    sessions, service, _, executor = await make_api_app(monkeypatch, tmp_path)
    task = await service.create_task(await sessions.create_session(), "Redis")
    cases = {
        "outline_invalid": 422,
        "draft_invalid": 422,
        "citation_invalid": 422,
        "provider_failed": 502,
        "model_unconfigured": 503,
        "storage_unavailable": 503,
        "deadline_exceeded": 504,
    }
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for code, expected_status in cases.items():
            executor.error = WritingExecutionError(code)
            response = await client.post(
                f"/api/writing-tasks/{task.task_id}/draft",
                json={"version": task.version},
            )
            assert response.status_code == expected_status
            assert response.json() == {"detail": {"code": code}}


@pytest.mark.asyncio
async def test_draft_attempt_api_returns_latest_draft_attempt_only(monkeypatch, tmp_path):
    sessions, service, execution, _ = await make_api_app(monkeypatch, tmp_path)
    task = await service.create_task(await sessions.create_session(), "Redis")
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        empty = await client.get(f"/api/writing-tasks/{task.task_id}/draft-attempt")
        missing = await client.get(f"/api/writing-tasks/{uuid4()}/draft-attempt")

    assert empty.status_code == 200
    assert empty.json() is None
    assert missing.status_code == 404

    citation = CitationSnapshot(
        citation_id="chunk-1",
        chunk_id="chunk-1",
        document_id="doc-1",
        document_version="v1",
        source_path="redis.md",
        source_url=None,
        title="Redis",
        heading_path="过期",
        start_line=1,
        end_line=1,
        text="设置过期时间。",
    )
    task = await service.publish_outline(
        task.task_id,
        {
            "title": "Redis",
            "sections": [
                {"title": "过期", "points": ["设置期限"], "citation_ids": ["chunk-1"]}
            ],
            "gaps": [],
        },
        [citation],
    )
    task = await service.confirm_outline(
        task.task_id,
        expected_version=task.version,
        outline=task.outline,
    )
    attempt = await execution.start_draft_attempt(
        task_id=task.task_id,
        expected_version=task.version,
        config=DraftConfig(),
        model_id="fake-model",
    )
    assert attempt.status == "running"
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"/api/writing-tasks/{task.task_id}/draft-attempt")
        assert response.status_code == 200
        assert response.json()["kind"] == "draft"
        assert response.json()["task_id"] == str(task.task_id)


@pytest.mark.asyncio
async def test_draft_api_does_not_generate_before_outline_confirmation(monkeypatch, tmp_path):
    sessions, service, _, executor = await make_api_app(
        monkeypatch, tmp_path, enforce_draft_status=True
    )
    task = await service.create_task(await sessions.create_session(), "Redis")
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/writing-tasks/{task.task_id}/draft",
            json={"version": task.version},
        )

    assert response.status_code == 409
    assert executor.calls == [(task.task_id, task.version)]
    current = await service.get_task(task.task_id)
    assert current.status == WritingStatus.RESEARCHING