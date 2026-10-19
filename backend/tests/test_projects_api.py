import httpx
import pytest

from app.application import create_app
from app.settings import Settings


@pytest.mark.asyncio
async def test_projects_api_browse_open_create_and_reject_outside_path(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = home / "existing"
    project.mkdir()
    config = Settings(_env_file=None, database_path=tmp_path / "agent.db", docs_root=tmp_path,
        draft_directory=tmp_path / "drafts", code_task_workspace_root=tmp_path / "workspaces",
        code_task_artifact_root=tmp_path / "artifacts", workspace_allowed_roots=[home])
    app = create_app(config)
    app.state.project_service.environment_id = "Ubuntu"
    app.state.project_service.service_instance_id = "instance-1"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        listing = await client.get("/api/projects/directories", params={"path": str(home)})
        assert listing.status_code == 200
        assert [item["name"] for item in listing.json()["directories"]] == ["existing"]
        opened = await client.post("/api/projects/open", json={"path": str(project)})
        assert opened.status_code == 201
        assert opened.json()["environment_id"] == "Ubuntu"
        repeated = await client.post("/api/projects/open", json={"path": str(project)})
        assert repeated.status_code == 200
        registered = await client.get("/api/workspaces")
        assert registered.status_code == 200
        assert len(registered.json()["workspaces"]) == 1
        created = await client.post("/api/projects", json={"parent_path": str(home), "name": "new"})
        assert created.status_code == 201
        conflict = await client.post("/api/projects", json={"parent_path": str(home), "name": "new"})
        assert conflict.status_code == 409
        denied = await client.post("/api/projects/open", json={"path": str(tmp_path)})
        assert denied.status_code == 403
