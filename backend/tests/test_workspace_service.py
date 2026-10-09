import json
import shutil

import pytest

from app.workspaces.service import WorkspacePathError, WorkspaceService


@pytest.mark.asyncio
async def test_workspace_create_copies_input_into_repo_and_lists_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("print('ok')\n")
    (source / "nested").mkdir()
    (source / "nested" / "test_app.py").write_text("def test_ok(): pass\n")
    service = WorkspaceService(tmp_path / "workspaces")

    workspace = await service.create(source)

    assert workspace.workspace_id
    assert workspace.repo_path == (tmp_path / "workspaces" / workspace.workspace_id / "repo").resolve()
    assert await service.list_files(workspace.workspace_id) == ["app.py", "nested/test_app.py"]
    assert await service.read_file(workspace.workspace_id, "app.py", 100) == "print('ok')\n"


@pytest.mark.asyncio
async def test_workspace_rejects_absolute_parent_and_symlink_escape(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "safe.txt").write_text("safe")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (source / "escape.txt").symlink_to(outside)
    service = WorkspaceService(tmp_path / "workspaces")
    workspace = await service.create(source)

    for path in ("/etc/passwd", "../outside.txt", "nested/../../outside.txt"):
        with pytest.raises(WorkspacePathError):
            service.resolve(workspace.workspace_id, path)
    with pytest.raises(WorkspacePathError):
        service.resolve(workspace.workspace_id, "escape.txt")


@pytest.mark.asyncio
async def test_workspace_enforces_read_and_write_limits_and_diff_scope(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "tracked.txt").write_text("before\n")
    service = WorkspaceService(tmp_path / "workspaces", max_file_bytes=8)
    workspace = await service.create(source)

    with pytest.raises(ValueError, match="too large"):
        await service.write_file(workspace.workspace_id, "large.txt", "123456789")
    with pytest.raises(ValueError, match="too large"):
        await service.read_file(workspace.workspace_id, "tracked.txt", 3)

    await service.write_file(workspace.workspace_id, "tracked.txt", "after\n")
    await service.write_file(workspace.workspace_id, "new.txt", "new\n")
    diff = await service.diff(workspace.workspace_id)
    assert "tracked.txt" in diff
    assert "new.txt" in diff
    assert "outside.txt" not in diff


@pytest.mark.asyncio
async def test_workspace_cleanup_removes_repo_but_keeps_metadata(tmp_path):
    service = WorkspaceService(tmp_path / "workspaces")
    workspace = await service.create()
    await service.write_file(workspace.workspace_id, "file.txt", "content")

    await service.cleanup(workspace.workspace_id)

    assert not workspace.repo_path.exists()
    assert workspace.metadata_path.exists()
    with pytest.raises(KeyError):
        await service.list_files(workspace.workspace_id)


@pytest.mark.asyncio
async def test_workspace_cleanup_failure_records_event_and_raises(tmp_path, monkeypatch):
    service = WorkspaceService(tmp_path / "workspaces")
    workspace = await service.create()

    def fail_cleanup(path):
        raise OSError("permission denied")

    monkeypatch.setattr(shutil, "rmtree", fail_cleanup)
    with pytest.raises(OSError):
        await service.cleanup(workspace.workspace_id)
    metadata = json.loads(workspace.metadata_path.read_text())
    assert metadata["status"] == "active"
    assert metadata["events"][-1]["event_type"] == "workspace.cleanup_failed"
    assert workspace.repo_path.exists()


@pytest.mark.asyncio
async def test_workspace_rejects_invalid_ids_metadata_paths_and_linked_writes(tmp_path):
    service = WorkspaceService(tmp_path / "workspaces")
    workspace = await service.create()
    outside = tmp_path / "workspaces-extra"
    outside.mkdir()
    (workspace.repo_path / "escape").symlink_to(outside, target_is_directory=True)
    for path in ("escape/new.txt", "../metadata.json", "", "bad\x00name"):
        with pytest.raises(WorkspacePathError):
            await service.write_file(workspace.workspace_id, path, "x")
    for ident in ("../outside", "missing", "/tmp"):
        with pytest.raises((KeyError, WorkspacePathError)):
            service.resolve(ident, "file.txt")
    assert not (outside / "new.txt").exists()


@pytest.mark.asyncio
async def test_workspace_diff_baseline_survives_service_restart_and_handles_deletion(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "old.txt").write_text("old")
    service = WorkspaceService(tmp_path / "workspaces")
    workspace = await service.create(source)
    assert await service.diff(workspace.workspace_id) == ""
    (workspace.repo_path / "old.txt").unlink()
    restarted = WorkspaceService(service.root)
    diff = await restarted.diff(workspace.workspace_id)
    assert "--- a/old.txt" in diff
    assert "+++ /dev/null" in diff
    assert "\\ No newline at end of file" in diff


@pytest.mark.asyncio
async def test_workspace_rejects_binary_diff_and_hardlink_reads(tmp_path):
    service = WorkspaceService(tmp_path / "workspaces")
    workspace = await service.create()
    (workspace.repo_path / "binary").write_bytes(b"\x00\xff")
    with pytest.raises(ValueError, match="binary"):
        await service.diff(workspace.workspace_id)
    (workspace.repo_path / "binary").unlink()
    outside = tmp_path / "outside"
    outside.write_text("secret")
    (workspace.repo_path / "linked").hardlink_to(outside)
    with pytest.raises(WorkspacePathError):
        await service.read_file(workspace.workspace_id, "linked", 100)