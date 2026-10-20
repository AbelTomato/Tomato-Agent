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
async def test_projects_support_multiple_shared_directories_and_remove_only_associations(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    first = home / "first"
    second = home / "second"
    first.mkdir()
    second.mkdir()
    (first / "keep.txt").write_text("keep")
    service = ProjectService(tmp_path / "agent.db", [home], "Ubuntu", "instance-1")

    project = await service.create_project("Alpha")
    await service.add_directory(project["project_id"], first)
    await service.add_directory(project["project_id"], second)
    await service.add_directory(project["project_id"], first)
    other = await service.create_project("Beta")
    await service.add_directory(other["project_id"], first)

    assert [item["root_path"] for item in await service.list_directories(project["project_id"])] == [str(first), str(second)]
    assert [item["root_path"] for item in await service.list_directories(other["project_id"])] == [str(first)]
    await service.remove_directory(project["project_id"], first)
    assert first.is_dir()
    assert (first / "keep.txt").read_text() == "keep"
    assert (await service.list_directories(other["project_id"]))[0]["root_path"] == str(first)


@pytest.mark.asyncio
async def test_legacy_single_root_projects_migrate_idempotently(tmp_path):
    import aiosqlite

    home = tmp_path / "home"
    home.mkdir()
    root = home / "legacy"
    root.mkdir()
    service = ProjectService(tmp_path / "agent.db", [home], "Ubuntu", "instance-1")
    await service.initialize()
    legacy = {
        "project_id": "legacy-id", "workspace_id": "workspace-id", "display_name": "Legacy",
        "root_path": str(root), "environment_id": "Ubuntu", "service_instance_id": "instance-1",
        "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
        "device": root.stat().st_dev, "inode": root.stat().st_ino,
    }
    await service.repository.create(legacy)
    await service.initialize()
    await service.initialize()

    projects = await service.list_projects()
    assert len(projects) == 1
    assert projects[0]["project_id"] == "legacy-id"
    assert projects[0]["display_name"] == "Legacy"
    directories = await service.list_directories("legacy-id")
    assert [item["root_path"] for item in directories] == [str(root)]
    assert directories[0]["workspace_id"] == "workspace-id"
    async with aiosqlite.connect(tmp_path / "agent.db") as db:
        cursor = await db.execute("SELECT COUNT(*) FROM project_directories")
        assert (await cursor.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_unavailable_project_directory_is_reported_and_rename_is_supported(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    source = home / "source"
    source.mkdir()
    service = ProjectService(tmp_path / "agent.db", [home], "Ubuntu", "instance-1")
    project = await service.create_project("Before")
    await service.add_directory(project["project_id"], source)
    renamed = await service.rename_project(project["project_id"], "After")
    assert renamed["display_name"] == "After"

    source.rename(home / "renamed-away")
    entries = await service.list_directories(project["project_id"])
    assert entries[0]["available"] is False


@pytest.mark.asyncio
async def test_renamed_migrated_project_keeps_legacy_open_response_in_sync(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    root = home / "legacy"
    root.mkdir()
    service = ProjectService(tmp_path / "agent.db", [home], "Ubuntu", "instance-1")
    legacy = await service.open(root)
    await service.rename_project(legacy["project_id"], "Updated name")

    assert (await service.open(root))["display_name"] == "Updated name"


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
