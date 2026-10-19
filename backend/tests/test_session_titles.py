import asyncio
from uuid import UUID

import pytest

from app.agent.models import LLMResponse
from test_unified_tasks_api import api, body


class FakeTitleClient:
    def __init__(self, response=None, error=None):
        self.response = response or LLMResponse(kind="final", content="Useful title")
        self.error = error
        self.calls = []

    async def complete(self, messages, tools):
        self.calls.append((messages, tools))
        if self.error:
            raise self.error
        return self.response


async def test_title_metadata_rename_and_legacy_metadata_compatibility(tmp_path):
    async with api(tmp_path) as (app, client, session):
        repo = app.state.session_repository
        await repo.set_title(UUID(session), "Legacy title", source="manual")
        listed = (await client.get("/api/sessions")).json()["sessions"][0]
        assert listed["title"] == "Legacy title"
        assert listed["title_source"] == "manual"
        assert listed["title_version"] == 1

        renamed = await client.patch(f"/api/sessions/{session}/title",
            json={"title": "Renamed", "expected_version": 1})
        assert renamed.status_code == 200
        assert renamed.json()["metadata"]["title"] == "Renamed"
        assert renamed.json()["title_source"] == "manual"
        stale = await client.patch(f"/api/sessions/{session}/title",
            json={"title": "Late title", "expected_version": 1})
        assert stale.status_code == 409
        assert (await repo.get_session(UUID(session))).metadata["title"] == "Renamed"


async def test_generated_title_validation_and_late_response_cas(tmp_path):
    async with api(tmp_path) as (app, _, session):
        repo = app.state.session_repository
        temporary = await repo.ensure_temporary_title(UUID(session), "first question")
        assert temporary.metadata["title"] == "first question"
        assert await repo.set_generated_title(UUID(session), "A\n" + "x" * 40, 0)
        generated = await repo.get_session(UUID(session))
        assert generated.metadata["title"] == "A"
        assert generated.title_source == "model"
        assert generated.title_version == 1
        assert not await repo.set_generated_title(UUID(session), "Too late", 0)

        renamed = await repo.set_title(UUID(session), "Human name", expected_version=1)
        assert renamed.title_source == "manual"
        assert not await repo.set_generated_title(UUID(session), "Late model", 2)
        assert (await repo.get_session(UUID(session))).metadata["title"] == "Human name"


async def test_title_generation_is_opt_in_bounded_and_failure_isolated():
    from app.sessions.titles import SessionTitleService

    client = FakeTitleClient()
    service = SessionTitleService(None, client, enabled=False)
    assert await service.title_for("question", "answer") is None
    assert not client.calls

    service = SessionTitleService(None, client, enabled=True)
    assert await service.title_for("question", "answer") == "Useful title"
    assert len(client.calls) == 1
    assert len(client.calls[0][0]) == 2

    async def slow(*_):
        await asyncio.sleep(0.05)

    client.complete = slow
    service = SessionTitleService(None, client, enabled=True, timeout_seconds=0.001)
    assert await service.title_for("question", "answer") is None

    service = SessionTitleService(None, FakeTitleClient(error=RuntimeError()), enabled=True)
    assert await service.title_for("question", "answer") is None


async def test_first_task_title_and_project_link_survive_session_repository_reload(tmp_path):
    async with api(tmp_path) as (app, client, session):
        root = tmp_path / "project"
        root.mkdir()
        workspace = await app.state.workspace_service.register_original(root, [])
        project = await client.post("/api/projects/open", json={"path": str(root)})
        assert project.status_code == 201
        await app.state.session_repository.set_default_workspace(UUID(session), workspace.workspace_id)

        submitted = await client.post(f"/api/sessions/{session}/tasks", json=body())
        assert submitted.status_code == 202
        record = await app.state.session_repository.get_session(UUID(session))
        assert record.metadata["title"] == "hello"
        assert record.project_id == project.json()["project_id"]

        await app.state.session_repository.init()
        restored = (await client.get("/api/sessions")).json()["sessions"][0]
        assert restored["title"] == "hello"
        assert restored["project_id"] == project.json()["project_id"]


async def test_session_project_link_requires_available_registered_project(tmp_path):
    async with api(tmp_path) as (app, client, session):
        root = tmp_path / "project"
        root.mkdir()
        project = await client.post("/api/projects/open", json={"path": str(root)})
        assert project.status_code == 201
        linked = await client.patch(f"/api/sessions/{session}/project",
            json={"project_id": project.json()["project_id"]})
        assert linked.status_code == 200
        assert linked.json()["project_id"] == project.json()["project_id"]
        rejected = await client.patch(f"/api/sessions/{session}/project",
            json={"project_id": "unknown"})
        assert rejected.status_code == 404