import json

import pytest
import pytest_asyncio

from app.agent.policies import CODE_TASK_TOOLS, CodeTaskCapabilityProfile
from app.artifacts.service import ArtifactService
from app.runs.repository import RunRepository
from app.tools.base import ToolContext
from app.tools.code_workspace import create_code_workspace_registry
from app.tools.registry import ToolRegistry
from app.workspaces.service import WorkspaceService


@pytest_asyncio.fixture
async def environment(tmp_path):
    workspaces = WorkspaceService(tmp_path / "workspaces")
    workspace = await workspaces.create()
    artifacts = ArtifactService(tmp_path / "artifacts", workspaces)
    await artifacts.init()
    runs = RunRepository(tmp_path / "runs.db")
    await runs.init()
    run = await runs.create_run("code", {"task": "fix"}, workspace.workspace_id)
    profile = CodeTaskCapabilityProfile(allowed_paths=(str(workspace.repo_path),))
    context = ToolContext(session_id="code", run_id=str(run.id),
                          workspace_id=workspace.workspace_id, profile=profile)
    registry = create_code_workspace_registry(workspaces, artifacts, runs)
    return registry, context, workspaces, workspace, artifacts, runs


@pytest.mark.asyncio
async def test_workspace_tools_read_write_search_diff_and_artifact(environment):
    registry, context, _, workspace, artifacts, _ = environment
    written = await registry.execute("write_file", {"path": "src/main.py", "content": "old\n"}, context)
    assert written.success
    listing = await registry.execute("list_files", {}, context)
    assert listing.data["files"] == ["src/main.py"]
    read = await registry.execute("read_file", {"path": "src/main.py"}, context)
    assert read.success and read.data["content"] == "old\n"
    search = await registry.execute("search_files", {"query": "old"}, context)
    assert search.success
    assert search.data["matches"] == [{"path": "src/main.py", "line": 1, "text": "old"}]
    patched = await registry.execute("apply_patch", {"changes": [
        {"path": "src/main.py", "old_text": "old", "new_text": "new"}
    ]}, context)
    assert patched.success
    diff = await registry.execute("get_diff", {}, context)
    assert diff.success and "+new" in diff.data["diff"]
    diff_ref = await artifacts.get(diff.data["artifact"]["artifact_id"])
    assert str(diff_ref.run_id) == context.run_id
    assert b"+new" in await artifacts.read(diff_ref.artifact_id, 10000)
    collected = await registry.execute("collect_artifact", {"path": "src/main.py"}, context)
    assert collected.success
    ref = await artifacts.get(collected.data["artifact"]["artifact_id"])
    assert ref.workspace_id == workspace.workspace_id
    assert str(ref.run_id) == context.run_id
    assert await artifacts.read(ref.artifact_id, 100) == b"new\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.txt", "src/../../outside.txt"])
async def test_tools_reject_unsafe_paths(environment, path):
    registry, context, _, _, _, _ = environment
    for name, args in [
        ("read_file", {"path": path}),
        ("write_file", {"path": path, "content": "bad"}),
        ("list_files", {"path": path}),
        ("search_files", {"path": path, "query": "x"}),
        ("collect_artifact", {"path": path}),
        ("apply_patch", {"changes": [{"path": path, "old_text": "x", "new_text": "y"}]}),
    ]:
        result = await registry.execute(name, args, context)
        assert not result.success


@pytest.mark.asyncio
async def test_tools_fail_closed_without_context_or_write_authorization(environment):
    registry, context, _, _, _, _ = environment
    no_profile = context.model_copy(update={"profile": None})
    assert not (await registry.execute("list_files", {}, no_profile)).success
    for updates in [{"workspace_id": None}, {"profile": {}},
                    {"profile": context.profile.model_copy(update={"allow_network": True})}]:
        assert not (await registry.execute("list_files", {}, context.model_copy(update=updates))).success
    read_only = context.model_copy(update={"profile": context.profile.model_copy(
        update={"allowed_tools": frozenset({"list_files", "read_file", "search_files", "get_diff"})}
    )})
    for name, args in [
        ("write_file", {"path": "x", "content": "bad"}),
        ("apply_patch", {"changes": [{"path": "x", "old_text": "x", "new_text": "y"}]}),
        ("collect_artifact", {"path": "x"}),
    ]:
        result = await registry.execute(name, args, read_only)
        assert not result.success and result.error == "permission_denied"
    wrong_root = context.model_copy(update={"profile": context.profile.model_copy(
        update={"allowed_paths": ("/unrelated",)}
    )})
    assert not (await registry.execute("list_files", {}, wrong_root)).success


@pytest.mark.asyncio
async def test_patch_validation_precedes_writes_and_rejects_shell(environment):
    registry, context, workspaces, workspace, _, _ = environment
    await workspaces.write_file(workspace.workspace_id, "a.txt", "old\n")
    await workspaces.write_file(workspace.workspace_id, "b.txt", "same same\n")
    for arguments in [
        {"patch": "echo unsafe"},
        {"changes": [{"path": "a.txt", "old_text": "missing", "new_text": "new"}]},
        {"changes": [
            {"path": "a.txt", "old_text": "old", "new_text": "new"},
            {"path": "b.txt", "old_text": "same", "new_text": "bad"},
        ]},
        {"changes": [{"path": "a.txt", "old_text": "", "new_text": "new"}]},
    ]:
        result = await registry.execute("apply_patch", arguments, context)
        assert not result.success and result.error == "invalid_arguments"
        assert await workspaces.read_file(workspace.workspace_id, "a.txt", 100) == "old\n"


@pytest.mark.asyncio
async def test_context_ownership_cannot_be_overridden_by_arguments(environment):
    registry, context, workspaces, _, artifacts, runs = environment
    other = await workspaces.create()
    await workspaces.write_file(other.workspace_id, "secret.txt", "secret")
    forged = context.model_copy(update={"workspace_id": other.workspace_id,
        "profile": CodeTaskCapabilityProfile(allowed_paths=(str(other.repo_path),))})
    result = await registry.execute("collect_artifact", {"path": "secret.txt"}, forged)
    assert not result.success and result.error == "permission_denied"
    result = await registry.execute("write_file", {"path": "x", "content": "bad",
        "workspace_id": other.workspace_id}, context)
    assert not result.success and result.error == "invalid_arguments"
    unknown = context.model_copy(update={"run_id": "unknown"})
    assert not (await registry.execute("list_files", {}, unknown)).success
    assert await artifacts.list_for_run(context.run_id) == []


@pytest.mark.asyncio
async def test_output_limits_are_explicit_and_keep_full_diff_artifact(environment):
    registry, context, workspaces, workspace, artifacts, _ = environment
    await workspaces.write_file(workspace.workspace_id, "long.txt", "needle " * 500)
    limited = context.model_copy(update={"profile": context.profile.model_copy(
        update={"max_output_chars": 600}
    )})
    for name, arguments in [
        ("read_file", {"path": "long.txt"}),
        ("search_files", {"query": "needle"}),
        ("get_diff", {}),
    ]:
        result = await registry.execute(name, arguments, limited)
        assert result.success and result.data["truncated"] is True
        assert len(json.dumps(result.data, ensure_ascii=False)) <= 600
        if name == "get_diff":
            assert len(await artifacts.read(result.data["artifact"]["artifact_id"], 10000)) > 600
    tiny = context.model_copy(update={"profile": context.profile.model_copy(
        update={"max_output_chars": 1}
    )})
    result = await registry.execute("read_file", {"path": "long.txt"}, tiny)
    assert not result.success and result.error == "output_limit"


@pytest.mark.asyncio
async def test_code_registry_declares_actual_capabilities_and_is_opt_in(environment):
    registry, _, _, _, _, _ = environment
    assert {item.name for item in registry.definitions()} == CODE_TASK_TOOLS - {"run_tests"}
    declarations = {item.name: item for item in registry.declarations()}
    for name, declaration in declarations.items():
        assert declaration.capabilities == frozenset({"file", "resource"})
        assert declaration.side_effect == ("write" if name in {
            "write_file", "apply_patch", "collect_artifact", "get_diff"
        } else "read")
    assert ToolRegistry().definitions() == []


@pytest.mark.asyncio
async def test_symlink_and_file_size_rejected(environment, tmp_path):
    registry, context, _, workspace, _, _ = environment
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace.repo_path / "link").symlink_to(outside)
    assert not (await registry.execute("read_file", {"path": "link"}, context)).success
    assert not (await registry.execute("write_file", {"path": "huge", "content": "x" * 100001}, context)).success


@pytest.mark.asyncio
async def test_listing_truncation_is_explicit(environment):
    registry, context, workspaces, workspace, _, _ = environment
    for number in range(20):
        await workspaces.write_file(workspace.workspace_id, f"file-{number:02}.txt", "ok")
    limited = context.model_copy(update={"profile": context.profile.model_copy(
        update={"max_output_chars": 100}
    )})
    result = await registry.execute("list_files", {}, limited)
    assert result.success and result.data["truncated"] is True
    assert 0 < len(result.data["files"]) < 20
    assert len(json.dumps(result.data, ensure_ascii=False)) <= 100