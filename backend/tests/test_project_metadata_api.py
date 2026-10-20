import httpx
import pytest

from app.api.projects import router
from app.projects.service import ProjectService


@pytest.mark.asyncio
async def test_project_metadata_api_supports_shared_directory_associations(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    source = home / "source"
    source.mkdir()
    app = type("App", (), {})()
    app.state = type("State", (), {})()
    app.state.project_service = ProjectService(tmp_path / "agent.db", [home], "Ubuntu", "instance-1")
    from fastapi import FastAPI
    application = FastAPI()
    application.state.project_service = app.state.project_service
    application.include_router(router)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        created = await client.post("/api/projects/metadata", json={"display_name": "Named project"})
        assert created.status_code == 201
        project_id = created.json()["project_id"]
        renamed = await client.patch(f"/api/projects/metadata/{project_id}", json={"display_name": "Renamed project"})
        assert renamed.status_code == 200
        assert renamed.json()["display_name"] == "Renamed project"
        associated = await client.post(f"/api/projects/metadata/{project_id}/directories", json={"path": str(source)})
        assert associated.status_code == 201
        duplicate = await client.post(f"/api/projects/metadata/{project_id}/directories", json={"path": str(source)})
        assert duplicate.status_code == 200
        listed = await client.get(f"/api/projects/metadata/{project_id}/directories")
        assert listed.status_code == 200
        assert listed.json()["directories"][0]["available"] is True
        removed = await client.delete(f"/api/projects/metadata/{project_id}/directories", params={"path": str(source)})
        assert removed.status_code == 204
        assert source.is_dir()

        legacy = await client.post("/api/projects/open", json={"path": str(source)})
        assert legacy.status_code == 201
        assert legacy.json()["root_path"] == str(source)
