from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app import main
from app.agent.models import LLMResponse
from app.knowledge.service import CitationSnapshot
from app.sessions.repository import SessionRepository
from app.writing.execution_models import ExecutionAttempt, ExecutionConfig, WritingExecutionError
from app.writing.execution_repository import WritingExecutionRepository
from app.writing.repository import WritingRepository
from app.writing.service import WritingService


class FakeExecutor:
    def __init__(self, service: WritingService):
        self.service = service
        self.calls = []
        self.error = None

    async def execute_research(self, task_id, *, expected_version):
        self.calls.append((task_id, expected_version))
        if self.error:
            raise self.error
        return await self.service.get_task(task_id)


class FakeLLM:
    async def complete(self, messages, tools):
        return LLMResponse(kind="final", content="{}")


async def make_api_app(monkeypatch, tmp_path: Path):
    path = tmp_path / "writing-api.db"
    sessions = SessionRepository(path)
    await sessions.init()
    writing = WritingRepository(path)
    await writing.init()
    service = WritingService(writing, draft_directory=tmp_path / "drafts")
    execution = WritingExecutionRepository(path)
    await execution.init()
    executor = FakeExecutor(service)
    monkeypatch.setattr(main.app.state, "writing_service", service)
    monkeypatch.setattr(main.app.state, "writing_executor", executor)
    monkeypatch.setattr(main.app.state, "writing_execution_repository", execution)
    return sessions, service, execution, executor


@pytest.mark.asyncio
async def test_research_rejects_client_controlled_outline(monkeypatch, tmp_path):
    await make_api_app(monkeypatch, tmp_path)
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/writing-tasks/{uuid4()}/research",
            json={"version": 1, "outline": {"title": "伪造提纲"}},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_research_and_attempt_api_use_injected_dependencies(monkeypatch, tmp_path):
    sessions, service, execution, executor = await make_api_app(monkeypatch, tmp_path)
    session_id = await sessions.create_session()
    task = await service.create_task(session_id, "Redis")
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/writing-tasks/{task.task_id}/research", json={"version": task.version}
        )
        assert response.status_code == 200
        assert response.json()["task_id"] == str(task.task_id)
        assert executor.calls == [(task.task_id, task.version)]

        attempt = await client.get(f"/api/writing-tasks/{task.task_id}/research-attempt")
        assert attempt.status_code == 200
        assert attempt.json() is None

        unchanged = await client.get(f"/api/writing-tasks/{task.task_id}")
        assert unchanged.status_code == 200
        assert unchanged.json()["status"] == "researching"


@pytest.mark.asyncio
async def test_research_maps_stable_execution_errors(monkeypatch, tmp_path):
    sessions, service, _, executor = await make_api_app(monkeypatch, tmp_path)
    task = await service.create_task(await sessions.create_session(), "Redis")
    cases = {
        "evidence_insufficient": 422,
        "provider_failed": 502,
        "model_unconfigured": 503,
        "deadline_exceeded": 504,
        "storage_unavailable": 503,
    }
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for code, expected_status in cases.items():
            executor.error = WritingExecutionError(code)
            response = await client.post(
                f"/api/writing-tasks/{task.task_id}/research", json={"version": task.version}
            )
            assert response.status_code == expected_status
            assert response.json() == {"detail": {"code": code}}


def test_writing_execution_settings_are_validated():
    assert main.Settings().writing_retrieval_mode == "keyword"
    assert main.Settings().writing_max_evidence == 5
    with pytest.raises(ValueError):
        main.Settings(writing_max_evidence=0)
    with pytest.raises(ValueError):
        main.Settings(writing_retrieval_mode="invalid")