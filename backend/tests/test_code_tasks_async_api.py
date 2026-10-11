from uuid import UUID

import httpx
import pytest
from unittest.mock import AsyncMock

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
async def test_recover_continue_returns_quickly_without_running_inline(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    fake_llm = CountingLLM()
    recover_calls = []

    async def recover(run_id, *, expected_version, action):
        recover_calls.append((run_id, expected_version, action))
        return {"status": "queued", "recovery_reason": "snapshot_resumable"}

    async with app.router.lifespan_context(app):
        app.state.code_task_llm = fake_llm
        service = app.state.code_task_service
        monkeypatch.setattr(service, "recover", recover)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post("/api/code-tasks", json={"task": "fix calculator"})
            run_id = created.json()["run_id"]
            response = await client.post(
                f"/api/code-tasks/{run_id}/recover",
                json={"expected_version": 0, "action": "continue", "confirm": True},
            )

    assert response.status_code == 200
    assert response.json() == {
        "status": "queued",
        "recovery_reason": "snapshot_resumable",
        "run_id": run_id,
        "dispatch_status": "worker_disabled",
    }
    assert recover_calls == [(run_id, 0, "continue")]
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


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["ready_model", "pending_tool", "ready_finish", "waiting"])
async def test_status_returns_bounded_snapshot_and_cursor_without_reading_events(tmp_path, monkeypatch, phase):
    app = make_app(tmp_path)
    async with app.router.lifespan_context(app):
        service = app.state.code_task_service
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            run_id = (await client.post("/api/code-tasks", json={"task": "private task"})).json()["run_id"]
            lease = await service.execution_repository.claim(UUID(run_id), "contract-test", lease_seconds=30)
            checkpoint = await service.run_repository.get_checkpoint(run_id)
            state = {**checkpoint.state, "phase": phase, "logical_index": 7,
                     "messages": [{"role": "user", "content": "private context"}],
                     "pending_response": {"content": "private response"},
                     "harness_state": {"secret": "private harness"},
                     "pending_decision": {"arguments": {"secret": "private input"}}}
            await service.execution_repository.commit_checkpoint(lease, state)
            checkpoint = await service.run_repository.get_checkpoint(run_id)
            events = (await client.get(f"/api/code-tasks/{run_id}/events")).json()["events"]
            monkeypatch.setattr(service.run_repository, "list_events", AsyncMock(
                side_effect=AssertionError("status must not load event history")))

            for method, path in [(client.get, f"/api/code-tasks/{run_id}"),
                                 (client.post, f"/api/code-tasks/{run_id}/execute")]:
                response = await method(path)
                assert response.status_code in {200, 202}
                payload = response.json()
                assert "events" not in payload
                assert payload["event_cursor"] == events[-1]["sequence"]
                assert payload["event_cursor"] > state["last_event_sequence"]
                assert payload["snapshot_summary"] == {
                    "sequence": checkpoint.sequence,
                    "created_at": checkpoint.created_at.isoformat(),
                    "protocol_version": state["protocol_version"],
                    "schema_version": state["schema_version"],
                    "checkpoint_revision": state["checkpoint_revision"],
                    "last_event_sequence": state["last_event_sequence"],
                    "phase": phase,
                    "logical_index": 7,
                }
                assert "private" not in response.text
            service.run_repository.list_events.assert_not_called()


@pytest.mark.asyncio
async def test_status_without_checkpoint_or_events_has_explicit_defaults(tmp_path):
    app = make_app(tmp_path)
    async with app.router.lifespan_context(app):
        run = await app.state.code_task_service.run_repository.create_run("code_task", {}, "legacy")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            response = await client.get(f"/api/code-tasks/{run.id}")
            assert response.status_code == 200
            assert response.json()["snapshot_summary"] is None
            assert response.json()["event_cursor"] == 0
            assert "events" not in response.json()