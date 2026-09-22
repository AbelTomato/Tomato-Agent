from pathlib import Path

import httpx
import pytest

from app import main
from app.sessions.repository import SessionRepository
from app.writing.repository import WritingRepository
from app.writing.service import WritingService
from uuid import uuid4


@pytest.mark.asyncio
async def test_writing_api_requires_explicit_confirmation_and_rejects_stale_version(
    monkeypatch, tmp_path: Path
):
    session_repo = SessionRepository(tmp_path / "agent.db")
    await session_repo.init()
    writing_repo = WritingRepository(tmp_path / "agent.db")
    await writing_repo.init()
    monkeypatch.setattr(main, "repo", session_repo)
    monkeypatch.setattr(main, "writing_repository", writing_repo)
    monkeypatch.setattr(main, "writing_service", main.WritingService(writing_repo))
    monkeypatch.setattr(main.app.state, "writing_service", main.writing_service)

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        session_id = str(await session_repo.create_session())
        created = await client.post(
            f"/api/sessions/{session_id}/writing-tasks",
            json={"topic": "Redis", "status": "saved"},
        )
        assert created.status_code == 200
        task = created.json()
        assert task["status"] == "researching"

        published = await main.writing_service.publish_outline(
            task["task_id"], {"sections": ["过期"]}, []
        )
        current = await client.get(f"/api/writing-tasks/{task['task_id']}")
        assert current.status_code == 200
        assert current.json()["status"] == "awaiting_outline_confirmation"

        stale = await client.post(
            f"/api/writing-tasks/{task['task_id']}/confirm-outline",
            json={"version": published.version - 1, "outline": {"sections": ["错误"]}},
        )
        assert stale.status_code == 409

        confirmed = await client.post(
            f"/api/writing-tasks/{task['task_id']}/confirm-outline",
            json={"version": published.version, "outline": {"sections": ["确认"]}},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "drafting"


@pytest.mark.asyncio
async def test_writing_save_api_only_accepts_version_and_idempotency_key(
    monkeypatch, tmp_path: Path
):
    session_repo = SessionRepository(tmp_path / "agent.db")
    await session_repo.init()
    writing_repo = WritingRepository(tmp_path / "agent.db")
    await writing_repo.init()
    session_id = await session_repo.create_session()
    service = WritingService(writing_repo, draft_directory=tmp_path / "drafts")
    monkeypatch.setattr(main, "repo", session_repo)
    monkeypatch.setattr(main, "writing_repository", writing_repo)
    monkeypatch.setattr(main, "writing_service", service)
    monkeypatch.setattr(main.app.state, "writing_service", service)

    task = await service.create_task(session_id, "Redis")
    task = await service.publish_outline(task.task_id, {"sections": ["过期"]}, [])
    task = await service.confirm_outline(
        task.task_id, expected_version=task.version, outline={"sections": ["过期"]}
    )
    task = await service.publish_draft(
        task.task_id,
        expected_version=task.version,
        draft="草稿正文",
        drafting_run_id=uuid4(),
    )

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        saved = await client.post(
            f"/api/writing-tasks/{task.task_id}/save",
            json={"version": task.version, "idempotency_key": "save-1", "path": "/tmp/escape.md"},
        )
        repeated = await client.post(
            f"/api/writing-tasks/{task.task_id}/save",
            json={"version": task.version, "idempotency_key": "save-1"},
        )
        different = await client.post(
            f"/api/writing-tasks/{task.task_id}/save",
            json={"version": task.version, "idempotency_key": "save-2"},
        )

    assert saved.status_code == 200
    assert saved.json()["status"] == "saved"
    assert saved.json()["saved_path"].startswith(str(tmp_path / "drafts"))
    assert repeated.status_code == 200
    assert repeated.json() == saved.json()
    assert different.status_code == 409


@pytest.mark.asyncio
async def test_retry_api_does_not_retry_save_failure(monkeypatch, tmp_path: Path):
    session_repo = SessionRepository(tmp_path / "agent.db")
    await session_repo.init()
    writing_repo = WritingRepository(tmp_path / "agent.db")
    await writing_repo.init()
    session_id = await session_repo.create_session()
    service = WritingService(writing_repo, draft_directory=tmp_path / "drafts")
    monkeypatch.setattr(main, "writing_service", service)
    monkeypatch.setattr(main.app.state, "writing_service", service)

    task = await service.create_task(session_id, "Redis")
    failed = await service.fail(
        task.task_id,
        expected_version=task.version,
        failed_stage="saving",
    )

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/writing-tasks/{task.task_id}/retry",
            json={"version": failed.version},
        )

    assert response.status_code == 409