from pathlib import Path

import pytest

from app.projects.service import ProjectPathError, ProjectService


@pytest.mark.asyncio
async def test_project_browse_open_create_and_restart_are_idempotent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    existing = home / "existing"
    existing.mkdir()
    service = ProjectService(tmp_path / "agent.db", [home], "Ubuntu", "instance-1")

    listing = await service.browse(home)
    assert [entry["name"] for entry in listing["directories"]] == ["existing"]
    record = await service.open(existing)
    assert await service.open(existing) == record
    assert record["environment_id"] == "Ubuntu"
    assert record["service_instance_id"] == "instance-1"
    assert record["workspace_id"]

    created = await service.create(home, "New 项目")
    (created["root_path"] and (home / "New 项目")).joinpath("README.md").write_text("keep")
    restarted = ProjectService(tmp_path / "agent.db", [home], "Ubuntu", "instance-1")
    assert await restarted.open(home / "New 项目") == created
    assert (home / "New 项目" / "README.md").read_text() == "keep"


@pytest.mark.asyncio
async def test_project_paths_names_links_and_missing_directories_are_rejected(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "escape").symlink_to(outside, target_is_directory=True)
    service = ProjectService(tmp_path / "agent.db", [home], "Ubuntu", "instance-1")

    for name in ("", ".", "..", "../outside", "/tmp/absolute", "nested/name", "nested\\name"):
        with pytest.raises((ValueError, ProjectPathError)):
            await service.create(home, name)
    with pytest.raises(ProjectPathError):
        await service.open(home / "escape")
    with pytest.raises(ProjectPathError):
        await service.open(outside)
    missing = home / "removed"
    with pytest.raises(ProjectPathError):
        await service.open(missing)


@pytest.mark.asyncio
async def test_project_create_conflict_does_not_overwrite_existing_content(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    existing = home / "project"
    existing.mkdir()
    (existing / "data.txt").write_text("original")
    service = ProjectService(tmp_path / "agent.db", [home], "Ubuntu", "instance-1")

    with pytest.raises(FileExistsError):
        await service.create(home, "project")
    assert (existing / "data.txt").read_text() == "original"


@pytest.mark.asyncio
async def test_project_keeps_directory_when_database_registration_fails(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    service = ProjectService(tmp_path / "agent.db", [home], "Ubuntu", "instance-1")
    await service.initialize()

    async def fail_insert(record):
        raise OSError("database unavailable")

    monkeypatch.setattr(service.repository, "create", fail_insert)
    with pytest.raises(OSError, match="database unavailable"):
        await service.create(home, "preserved")
    assert (home / "preserved").is_dir()


@pytest.mark.asyncio
async def test_project_registration_failure_does_not_remove_preexisting_workspace(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    project = home / "existing"
    project.mkdir()
    (project / "tests").mkdir()
    from app.workspaces.service import WorkspaceService
    workspaces = WorkspaceService(tmp_path / "workspaces", allowed_roots=[home])
    registered = await workspaces.register_original(project, ["tests"])
    service = ProjectService(tmp_path / "agent.db", [home], "Ubuntu", "instance-1", workspaces)
    await service.initialize()

    async def fail_insert(record):
        raise OSError("database unavailable")

    monkeypatch.setattr(service.repository, "create", fail_insert)
    with pytest.raises(OSError):
        await service.open(project)
    assert (await workspaces.get(registered.workspace_id)).pytest_paths == ("tests",)


@pytest.mark.asyncio
async def test_project_identity_is_scoped_to_environment_and_service_instance(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = home / "demo"
    project.mkdir()
    first = ProjectService(tmp_path / "agent.db", [home], "Ubuntu", "instance-1")
    other_environment = ProjectService(tmp_path / "agent.db", [home], "Debian", "instance-1")
    other_instance = ProjectService(tmp_path / "agent.db", [home], "Ubuntu", "instance-2")

    a = await first.open(project)
    b = await other_environment.open(project)
    c = await other_instance.open(project)
    assert len({a["project_id"], b["project_id"], c["project_id"]}) == 3


@pytest.mark.asyncio
async def test_open_reuses_existing_workspace_test_profile(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = home / "existing"
    project.mkdir()
    (project / "tests").mkdir()
    from app.workspaces.service import WorkspaceService
    workspaces = WorkspaceService(tmp_path / "workspaces", allowed_roots=[home])
    registered = await workspaces.register_original(project, ["tests"])
    projects = ProjectService(tmp_path / "agent.db", [home], "Ubuntu", "instance-1", workspaces)

    opened = await projects.open(project)
    assert opened["workspace_id"] == registered.workspace_id
    assert (await workspaces.get(registered.workspace_id)).pytest_paths == ("tests",)


@pytest.mark.asyncio
async def test_project_browser_does_not_follow_links_and_bounds_results(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").mkdir()
    (home / "escape").symlink_to(outside, target_is_directory=True)
    service = ProjectService(tmp_path / "agent.db", [home], "Ubuntu", "instance-1")
    for index in range(service.MAX_DIRECTORY_ENTRIES + 5):
        (home / f"project-{index:04d}").mkdir()

    listing = await service.browse(home)
    assert len(listing["directories"]) == service.MAX_DIRECTORY_ENTRIES - 1
    assert all(item["name"] != "escape" for item in listing["directories"])


@pytest.mark.asyncio
async def test_project_browse_reports_permission_error(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    service = ProjectService(tmp_path / "agent.db", [home], "Ubuntu", "instance-1")

    def denied(_path):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "iterdir", denied)
    with pytest.raises(PermissionError):
        await service.browse(home)
