from uuid import uuid4

import pytest
import pytest_asyncio

from app.runs.repository import RunRepository
from app.workspaces.service import WorkspaceService
from app.workspaces.run_changes import RunFileChanges, FileChangeConflict


@pytest_asyncio.fixture
async def env(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "tests").mkdir()
    (project / "tests/test_app.py").write_text("assert True\n")
    (project / "app.py").write_text("user changes\n")
    (project / "unrelated.py").write_text("other user changes\n")
    workspaces = WorkspaceService(tmp_path / "metadata", allowed_roots=[project])
    workspace = await workspaces.register_original(project, ["tests"])
    runs = RunRepository(tmp_path / "runs.db")
    await runs.init()
    run = await runs.create_run("code", {}, workspace.workspace_id)
    return project, workspaces, runs, str(run.id), workspace.workspace_id


@pytest.mark.asyncio
async def test_persistent_baseline_and_two_round_diffs(env):
    project, workspaces, runs, run, workspace = env
    changes = RunFileChanges(workspaces, runs)
    assert await changes.observe(run, workspace, "app.py") == "user changes\n"
    await changes.write_checked(run, workspace, "app.py", "first round\n")
    restored = RunFileChanges(workspaces, runs)
    diff = await restored.diff_for_run(run)
    assert "-user changes" in diff and "+first round" in diff
    assert "unrelated" not in diff
    second = str((await runs.create_run("code", {}, workspace)).id)
    await restored.observe(second, workspace, "app.py")
    await restored.write_checked(second, workspace, "app.py", "second round\n")
    assert "-first round" in await restored.diff_for_run(second)
    assert "second round" not in await restored.diff_for_run(run)
    assert await restored.conflicts_for_run(run) == ["app.py"]
    assert project.joinpath("unrelated.py").read_text() == "other user changes\n"


@pytest.mark.asyncio
async def test_missing_observation_and_external_edit_refuse_overwrite(env):
    project, workspaces, runs, run, workspace = env
    changes = RunFileChanges(workspaces, runs)
    with pytest.raises(FileChangeConflict, match="observation_required"):
        await changes.write_checked(run, workspace, "app.py", "bad")
    await changes.observe(run, workspace, "app.py")
    project.joinpath("app.py").write_text("external\n")
    with pytest.raises(FileChangeConflict, match="file_changed"):
        await changes.write_checked(run, workspace, "app.py", "bad")
    assert project.joinpath("app.py").read_text() == "external\n"


@pytest.mark.asyncio
async def test_new_file_requires_missing_observation_and_rejects_occupation(env):
    project, workspaces, runs, run, workspace = env
    changes = RunFileChanges(workspaces, runs)
    assert await changes.observe(run, workspace, "new.py") is None
    project.joinpath("new.py").write_text("external")
    with pytest.raises(FileChangeConflict):
        await changes.write_checked(run, workspace, "new.py", "agent")
    await changes.observe(run, workspace, "created.py")
    await changes.write_checked(run, workspace, "created.py", "agent\n")
    assert "new file mode" in await changes.diff_for_run(run)


@pytest.mark.asyncio
async def test_patch_prechecks_all_files_without_changing_first(env):
    project, workspaces, runs, run, workspace = env
    changes = RunFileChanges(workspaces, runs)
    for name in ["app.py", "unrelated.py"]:
        await changes.observe(run, workspace, name)
    project.joinpath("unrelated.py").write_text("external")
    with pytest.raises(FileChangeConflict):
        await changes.patch_checked(run, workspace, [
            {"path": "app.py", "old_text": "user", "new_text": "agent"},
            {"path": "unrelated.py", "old_text": "other", "new_text": "agent"},
        ])
    assert project.joinpath("app.py").read_text() == "user changes\n"


@pytest.mark.asyncio
async def test_atomic_replace_preserves_mode_and_agent_diff_excludes_external(env):
    project, workspaces, runs, run, workspace = env
    changes = RunFileChanges(workspaces, runs)
    path = project / "app.py"
    path.chmod(0o751)
    inode = path.stat().st_ino
    await changes.observe(run, workspace, "app.py")
    await changes.write_checked(run, workspace, "app.py", "agent\n")
    assert path.stat().st_ino != inode
    assert path.stat().st_mode & 0o777 == 0o751
    path.write_text("external\n")
    assert "+agent" in await changes.diff_for_run(run)
    assert "+external" not in await changes.diff_for_run(run)
    assert await changes.conflicts_for_run(run) == ["app.py"]


@pytest.mark.asyncio
async def test_manifest_covers_tests_binary_inputs_and_refuses_truncation(env):
    project, workspaces, runs, run, workspace = env
    changes = RunFileChanges(workspaces, runs)
    project.joinpath("data.bin").write_bytes(b"\x00\xff")
    project.joinpath(".env").write_text("secret")
    first = await changes.verification_manifest(run, workspace)
    assert "tests/test_app.py" in first["files"] and "data.bin" in first["files"]
    assert ".env" not in first["files"]
    project.joinpath("tests/test_app.py").write_text("assert False\n")
    assert (await changes.verification_manifest(run, workspace))["sha256"] != first["sha256"]
    workspaces.max_files = 1
    with pytest.raises(ValueError, match="enumeration_truncated"):
        await changes.verification_manifest(run, workspace)


@pytest.mark.asyncio
async def test_binding_and_unsafe_paths_are_rejected(env):
    _, workspaces, runs, run, workspace = env
    changes = RunFileChanges(workspaces, runs)
    with pytest.raises((KeyError, ValueError)):
        await changes.observe(str(uuid4()), workspace, "app.py")
    with pytest.raises(ValueError):
        await changes.observe(run, workspace, "../outside")


@pytest.mark.asyncio
async def test_original_tools_require_observation_and_report_conflicts(env, tmp_path):
    from app.agent.policies import CodeTaskCapabilityProfile
    from app.artifacts.service import ArtifactService
    from app.tools.base import ToolContext
    from app.tools.code_workspace import create_code_workspace_registry

    project, workspaces, runs, run, workspace = env
    artifacts = ArtifactService(tmp_path / "artifacts", workspaces)
    await artifacts.init()
    registry = create_code_workspace_registry(workspaces, artifacts, runs)
    context = ToolContext(session_id="code", run_id=run, workspace_id=workspace,
                          profile=CodeTaskCapabilityProfile(allowed_paths=(str(project),)))
    result = await registry.execute("write_file", {"path": "app.py", "content": "bad"}, context)
    assert not result.success and result.error == "observation_required"
    assert (await registry.execute("read_file", {"path": "app.py"}, context)).success
    assert (await registry.execute("write_file", {"path": "app.py", "content": "agent\n"}, context)).success
    project.joinpath("app.py").write_text("external")
    result = await registry.execute("get_diff", {}, context)
    assert result.success and result.data["conflicts"] == ["app.py"]
    assert "+agent" in result.data["diff"]


@pytest.mark.asyncio
async def test_uncommitted_write_requires_explicit_exited_executor_and_postimage(env, monkeypatch):
    project, workspaces, runs, run, workspace = env
    changes = RunFileChanges(workspaces, runs)
    await changes.observe(run, workspace, "app.py")
    original_update = changes._update

    async def crash_after_write(run_id, path, clause, values):
        if clause.startswith("agent="):
            raise RuntimeError("crash after replace")
        return await original_update(run_id, path, clause, values)

    monkeypatch.setattr(changes, "_update", crash_after_write)
    with pytest.raises(RuntimeError, match="crash"):
        await changes.write_checked(run, workspace, "app.py", "agent\n")
    restored = RunFileChanges(workspaces, runs)
    assert project.joinpath("app.py").read_text() == "agent\n"
    assert await restored.diff_for_run(run) == ""
    with pytest.raises(FileChangeConflict, match="executor_exit_required"):
        await restored.reconcile_pending(run, workspace, "app.py", executor_exited=False)
    assert await restored.reconcile_pending(run, workspace, "app.py", executor_exited=True) == "postimage"
    assert "+agent" in await restored.diff_for_run(run)


@pytest.mark.asyncio
async def test_pending_external_conflict_cannot_be_cleared_by_observe_or_reconcile(env, monkeypatch):
    project, workspaces, runs, run, workspace = env
    changes = RunFileChanges(workspaces, runs)
    await changes.observe(run, workspace, "app.py")

    def unknown(*args):
        project.joinpath("app.py").write_text("external\n")
        raise OSError("unknown result")

    monkeypatch.setattr(changes, "_replace", unknown)
    with pytest.raises(OSError):
        await changes.write_checked(run, workspace, "app.py", "agent\n")
    restored = RunFileChanges(workspaces, runs)
    with pytest.raises(FileChangeConflict, match="write_pending_review"):
        await restored.observe(run, workspace, "app.py")
    with pytest.raises(FileChangeConflict, match="file_changed"):
        await restored.reconcile_pending(run, workspace, "app.py", executor_exited=True)
    assert project.joinpath("app.py").read_text() == "external\n"
    assert await restored.diff_for_run(run) == ""


@pytest.mark.asyncio
async def test_original_execution_port_uses_observed_preimage(env):
    from app.agent.code_execution import CodeExecutionPort
    from types import SimpleNamespace

    project, workspaces, runs, run, workspace = env
    changes = RunFileChanges(workspaces, runs)
    # Only describe_operation is exercised; no claim or business database is needed.
    port = object.__new__(CodeExecutionPort)
    port.service = SimpleNamespace(workspace_service=workspaces, run_repository=runs)
    port.workspace_id = workspace
    port.lease = SimpleNamespace(run_id=run)
    await changes.observe(run, workspace, "app.py")
    project.joinpath("app.py").write_text("external")
    with pytest.raises(FileChangeConflict, match="file_changed"):
        await port.describe_operation(0, "write_file", {"path": "app.py", "content": "agent"})


@pytest.mark.asyncio
async def test_successful_patch_unique_match_and_cancel_preserve_files(env):
    project, workspaces, runs, run, workspace = env
    changes = RunFileChanges(workspaces, runs)
    for name in ["app.py", "unrelated.py"]:
        await changes.observe(run, workspace, name)
    with pytest.raises(ValueError, match="exactly once"):
        await changes.patch_checked(run, workspace, [
            {"path": "app.py", "old_text": "user", "new_text": "agent"},
            {"path": "unrelated.py", "old_text": "not found", "new_text": "agent"},
        ])
    assert project.joinpath("app.py").read_text() == "user changes\n"
    result = await changes.patch_checked(run, workspace, [
        {"path": "app.py", "old_text": "user", "new_text": "agent"},
        {"path": "unrelated.py", "old_text": "other", "new_text": "agent"},
    ])
    assert result == ["app.py", "unrelated.py"]
    await runs.update_status(run, "cancelled")
    assert project.joinpath("app.py").read_text() == "agent changes\n"
    assert "+agent changes" in await RunFileChanges(workspaces, runs).diff_for_run(run)


@pytest.mark.asyncio
async def test_final_digest_check_rejects_edit_after_preflight(env, monkeypatch):
    project, workspaces, runs, run, workspace = env
    changes = RunFileChanges(workspaces, runs)
    await changes.observe(run, workspace, "app.py")
    original_replace = changes._replace

    def external_edit(*args):
        project.joinpath("app.py").write_text("external\n")
        original_replace(*args)

    monkeypatch.setattr(changes, "_replace", external_edit)
    with pytest.raises(FileChangeConflict, match="file_changed"):
        await changes.write_checked(run, workspace, "app.py", "agent")
    assert project.joinpath("app.py").read_text() == "external\n"
    assert not list(project.glob(".agent-write-*"))


@pytest.mark.asyncio
async def test_reconcile_preimage_allows_retry_without_writing(env, monkeypatch):
    project, workspaces, runs, run, workspace = env
    changes = RunFileChanges(workspaces, runs)
    await changes.observe(run, workspace, "app.py")

    def before_replace(*args):
        raise OSError("stopped before replace")

    monkeypatch.setattr(changes, "_replace", before_replace)
    with pytest.raises(OSError):
        await changes.write_checked(run, workspace, "app.py", "agent")
    restored = RunFileChanges(workspaces, runs)
    assert await restored.reconcile_pending(run, workspace, "app.py", executor_exited=True) == "preimage"
    assert project.joinpath("app.py").read_text() == "user changes\n"
    await restored.write_checked(run, workspace, "app.py", "agent")
    assert project.joinpath("app.py").read_text() == "agent"