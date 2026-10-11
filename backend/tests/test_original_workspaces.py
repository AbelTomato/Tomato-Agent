import json
from pathlib import Path

import httpx
import pytest

from app.application import create_app
from app.settings import Settings
from app.workspaces.service import WorkspacePathError, WorkspaceService


def environment(tmp_path, **options):
    project = tmp_path / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "app.py").write_text("original\n")
    service = WorkspaceService(tmp_path / "metadata", allowed_roots=[tmp_path / "projects"], **options)
    return service, project


async def register(service, project):
    return await service.register_original(project, ["tests"])


async def test_registration_is_persistent_idempotent_and_does_not_touch_project(tmp_path):
    service, project = environment(tmp_path)
    before = sorted(project.rglob("*"))
    record = await register(service, project)
    assert record.mode == "original"
    assert record.repo_path == project
    assert record.pytest_paths == ("tests",)
    assert (record.device, record.inode) == (project.stat().st_dev, project.stat().st_ino)
    assert (await register(service, project)).workspace_id == record.workspace_id
    restarted = WorkspaceService(service.root, allowed_roots=service.allowed_roots)
    assert await restarted.get(record.workspace_id) == record
    assert restarted.root_path(record.workspace_id) == project
    assert sorted(project.rglob("*")) == before
    assert "baseline" not in json.loads(record.metadata_path.read_text())
    await restarted.unregister(record.workspace_id)
    await restarted.unregister(record.workspace_id)
    assert (project / "app.py").read_text() == "original\n"
    with pytest.raises(KeyError):
        restarted.root_path(record.workspace_id)


async def test_registration_denies_unconfigured_outside_and_non_directory_paths(tmp_path):
    service, project = environment(tmp_path)
    for path in [tmp_path, project / "app.py", project / "missing", Path("relative")]:
        with pytest.raises((ValueError, WorkspacePathError)):
            await register(service, path)
    with pytest.raises(WorkspacePathError):
        await register(WorkspaceService(service.root), project)
    with pytest.raises(ValueError):
        WorkspaceService(service.root, allowed_roots=[Path("/")])
    with pytest.raises(ValueError):
        Settings(_env_file=None, workspace_allowed_roots=[Path("/")])
    assert Settings(_env_file=None).workspace_allowed_roots == []


@pytest.mark.parametrize("target", ["-q", "../outside", "/etc", "missing", ".env", "tests/link"])
async def test_pytest_paths_are_validated_on_registration(tmp_path, target):
    service, project = environment(tmp_path)
    (project / ".env").write_text("secret")
    (project / "tests" / "link").symlink_to(project / "app.py")
    with pytest.raises((ValueError, WorkspacePathError)):
        await service.register_original(project, [target])
    with pytest.raises(ValueError):
        await service.register_original(project, [])


async def test_registration_rejects_links_in_root_and_ancestors(tmp_path):
    service, project = environment(tmp_path)
    alias = project.parent / "alias"
    alias.symlink_to(project, target_is_directory=True)
    with pytest.raises(WorkspacePathError):
        await register(service, alias)
    with pytest.raises(WorkspacePathError):
        await register(service, alias / "tests")


@pytest.mark.parametrize("change", ["missing", "replace", "link", "ancestor_link"])
async def test_root_identity_is_checked_on_every_access(tmp_path, change):
    service, project = environment(tmp_path)
    record = await register(service, project)
    if change == "ancestor_link":
        parent = project.parent
        saved = parent.with_name("saved-projects")
        parent.rename(saved)
        parent.symlink_to(saved, target_is_directory=True)
    else:
        saved = project.with_name("saved")
        project.rename(saved)
        if change == "replace":
            project.mkdir()
        elif change == "link":
            project.symlink_to(saved, target_is_directory=True)
    for access in [lambda: service.root_path(record.workspace_id),
                   lambda: service.resolve(record.workspace_id, "app.py")]:
        with pytest.raises(WorkspacePathError, match="workspace_changed"):
            access()
    with pytest.raises(WorkspacePathError, match="workspace_changed"):
        await service.get(record.workspace_id)


@pytest.mark.parametrize("path", ["../escape", "/etc/passwd", ".git/config", ".env", ".env.local",
    ".ssh/id_rsa", "keys/private.pem", "private.key", "credentials.json", "id_ed25519",
    ".aws/credentials", "nested/.env", "node_modules/file", ".venv/file", "__pycache__/file"])
async def test_same_filter_protects_read_write_and_enumeration(tmp_path, path):
    service, project = environment(tmp_path)
    record = await register(service, project)
    if not path.startswith(("/", "..")):
        file = project / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("secret")
    for write in [False, True]:
        with pytest.raises(WorkspacePathError):
            service.resolve(record.workspace_id, path, write=write)
    assert path not in await service.list_files(record.workspace_id)


async def test_templates_links_limits_and_cleanup_guard(tmp_path):
    service, project = environment(tmp_path, max_files=2)
    record = await register(service, project)
    for name in [".env.example", ".env.sample", ".env.template"]:
        (project / name).write_text("template")
        assert await service.read_file(record.workspace_id, name, 100) == "template"
    listing = await service.enumerate_files(record.workspace_id)
    assert len(listing.files) == 2 and listing.truncated is True
    with pytest.raises(ValueError, match="enumeration_truncated"):
        await service.list_files(record.workspace_id)
    with pytest.raises(ValueError, match="original"):
        await service.cleanup(record.workspace_id)
    with pytest.raises(ValueError, match="Run"):
        await service.diff(record.workspace_id)
    (project / "link").symlink_to(project / "app.py")
    with pytest.raises(WorkspacePathError):
        await service.read_file(record.workspace_id, "link", 100)
    (project / "large").write_bytes(b"x" * 100001)
    with pytest.raises(ValueError, match="too large"):
        await service.read_file(record.workspace_id, "large", 200000)
    with pytest.raises(ValueError, match="too large"):
        await service.write_file(record.workspace_id, "large", "x" * 100001)
    assert project.is_dir()


async def test_workspace_http_registration_query_and_unregister(tmp_path):
    _, project = environment(tmp_path)
    config = Settings(_env_file=None, database_path=tmp_path / "agent.db", docs_root=tmp_path,
        draft_directory=tmp_path / "drafts", code_task_workspace_root=tmp_path / "metadata",
        code_task_artifact_root=tmp_path / "artifacts", workspace_allowed_roots=[project.parent])
    app = create_app(config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/workspaces", json={"path": str(project), "pytest_paths": ["tests"]})
        assert created.status_code == 201
        record = created.json()
        assert record["mode"] == "original"
        url = f"/api/workspaces/{record['workspace_id']}"
        assert (await client.get(url)).json() == record
        assert (await client.get("/api/workspaces/bad-id")).status_code == 400
        assert (await client.post("/api/workspaces", json={"path": str(tmp_path), "pytest_paths": ["tests"]})).status_code == 403
        assert (await client.post("/api/workspaces", json={"path": str(project), "pytest_paths": ["-q"]})).status_code == 422
        project.rename(project.with_name("saved"))
        changed = await client.get(url)
        assert changed.status_code == 409 and changed.json()["detail"]["code"] == "workspace_changed"
        assert (await client.post(url + "/unregister")).status_code == 200
        assert (await client.get(url)).status_code == 404
    assert (project.with_name("saved") / "app.py").read_text() == "original\n"


async def test_managed_metadata_without_mode_remains_compatible(tmp_path):
    service = WorkspaceService(tmp_path / "metadata")
    record = await service.create()
    metadata = json.loads(record.metadata_path.read_text())
    metadata.pop("mode")
    record.metadata_path.write_text(json.dumps(metadata))
    assert (await service.get(record.workspace_id)).mode == "managed"
    assert service.root_path(record.workspace_id) == record.repo_path
    await service.write_file(record.workspace_id, "safe.py", "safe")
    assert "safe.py" in await service.diff(record.workspace_id)


async def test_reregister_requires_same_targets_and_preserves_existing_metadata(tmp_path):
    service, project = environment(tmp_path)
    record = await register(service, project)
    before = record.metadata_path.read_bytes()
    with pytest.raises(ValueError, match="different pytest_paths"):
        await service.register_original(project, ["app.py"])
    assert record.metadata_path.read_bytes() == before
    await service.unregister(record.workspace_id)
    replacement = await service.register_original(project, ["app.py"])
    assert replacement.workspace_id != record.workspace_id


async def test_enumeration_does_not_hide_inaccessible_subtrees(tmp_path, monkeypatch):
    service, project = environment(tmp_path)
    record = await register(service, project)

    def denied_walk(path, **kwargs):
        if kwargs.get("onerror"):
            kwargs["onerror"](PermissionError("denied subtree"))
        return iter(())

    monkeypatch.setattr("app.workspaces.service.os.walk", denied_walk)
    with pytest.raises(PermissionError, match="denied subtree"):
        await service.enumerate_files(record.workspace_id)