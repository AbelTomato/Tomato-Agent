from uuid import UUID

import httpx
import pytest

from app.application import create_app
from app.settings import Settings


class CountingLLM:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, tools):
        self.calls.append((messages, tools))
        raise AssertionError("the HTTP request must not execute the model")


def make_app(tmp_path, *, worker_enabled=False):
    return create_app(Settings(
        _env_file=None,
        database_path=tmp_path / "agent.db",
        docs_root=tmp_path,
        code_task_workspace_root=tmp_path / "workspaces",
        code_task_artifact_root=tmp_path / "artifacts",
        llm_api_key="",
        code_task_worker_enabled=worker_enabled,
        knowledge_pipeline_enabled=False,
        knowledge_rerank_enabled=False,
    ))


@pytest.mark.asyncio
async def test_create_returns_queued_without_calling_llm(tmp_path):
    app = make_app(tmp_path)
    fake_llm = CountingLLM()
    async with app.router.lifespan_context(app):
        app.state.code_task_llm = fake_llm
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/code-tasks", json={"task": "fix calculator"})

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["dispatch_status"] == "worker_disabled"
    assert fake_llm.calls == []


@pytest.mark.asyncio
async def test_execute_returns_accepted_without_running_inline(tmp_path):
    app = make_app(tmp_path)
    fake_llm = CountingLLM()
    async with app.router.lifespan_context(app):
        app.state.code_task_llm = fake_llm
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post("/api/code-tasks", json={"task": "fix calculator"})
            run_id = created.json()["run_id"]
            response = await client.post(f"/api/code-tasks/{run_id}/execute")

    assert response.status_code == 202
    assert response.json()["status"] in {"queued", "running"}
    assert fake_llm.calls == []


@pytest.mark.asyncio
async def test_event_cursor_returns_only_events_after_sequence(tmp_path):
    app = make_app(tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post("/api/code-tasks", json={"task": "fix calculator"})
            run_id = created.json()["run_id"]
            response = await client.get(f"/api/code-tasks/{run_id}/events?after=0")

    assert response.status_code == 200
    assert all(item["sequence"] > 0 for item in response.json()["events"])


@pytest.mark.asyncio
async def test_cancel_is_idempotent_for_queued_task(tmp_path):
    app = make_app(tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post("/api/code-tasks", json={"task": "fix calculator"})
            run_id = str(UUID(created.json()["run_id"]))
            first = await client.post(
                f"/api/code-tasks/{run_id}/cancel", json={"reason": "user"}
            )
            repeated = await client.post(
                f"/api/code-tasks/{run_id}/cancel", json={"reason": "user"}
            )

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert first.json()["status"] == repeated.json()["status"] == "cancelled"