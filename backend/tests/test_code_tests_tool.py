import asyncio
import json

import pytest
import pytest_asyncio

from app.agent.policies import CodeTaskCapabilityProfile
from app.agent.sandbox import LocalSandboxExecutor, SandboxResult
from app.artifacts.service import ArtifactService
from app.runs.repository import RunRepository
from app.tools.base import ToolContext
from app.tools.registry import ToolRegistry
from app.tools.code_tests import RegisteredTestTarget, RunTestsTool, TestTargetRegistry
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
    context = ToolContext(
        session_id="code", run_id=str(run.id), workspace_id=workspace.workspace_id,
        profile=CodeTaskCapabilityProfile(allowed_paths=(str(workspace.repo_path),)),
    )
    targets = TestTargetRegistry([RegisteredTestTarget(
        name="unit", command=("python", "-m", "pytest", "tests"),
        allowed_arguments=("-q",),
    )])
    calls = []

    async def backend(request, profile):
        calls.append((request, profile))
        return {"exit_code": 0, "stdout": "1 passed", "stderr": ""}

    tool = RunTestsTool(targets, LocalSandboxExecutor(backend), workspaces, artifacts, runs)
    return tool, context, artifacts, calls, workspace


@pytest.mark.asyncio
async def test_registered_target_mapping_and_report(environment):
    tool, context, artifacts, calls, workspace = environment
    result = await tool.execute({"target": "unit", "arguments": ["-q"]}, context)
    assert result.success and result.data["passed"] is True
    request, profile = calls[0]
    assert request.tool_name == "run_tests"
    assert request.arguments == {
        "target": "unit", "argv": ["python", "-m", "pytest", "tests", "-q"],
        "cwd": str(workspace.repo_path),
    }
    assert request.input_refs == (context.run_id, context.workspace_id)
    assert not profile.allow_network and not profile.allow_process
    assert profile.credential_names == ()
    ref = await artifacts.get(result.data["artifact"]["artifact_id"])
    assert str(ref.run_id) == context.run_id and ref.kind == "test_report"
    report = json.loads(await artifacts.read(ref.artifact_id, 10000))
    assert report["exit_code"] == 0 and report["passed"] is True
    assert report["workspace_id"] == context.workspace_id
    assert report["stdout"] == "1 passed"
    declaration = tool.declaration()
    assert declaration.capabilities == frozenset({"file", "process", "resource"})
    assert declaration.side_effect == "write"
    assert tool.definition().name == "run_tests"


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {"target": "unknown"}, {"target": "unit; touch /tmp/pwn"},
    {"target": "unit", "arguments": ["-q; echo bad"]},
    {"target": "unit", "arguments": ["$(id)"]},
    {"target": "unit", "arguments": ["--override-ini=x"]},
    {"target": "unit", "arguments": "-q"},
    *[{"target": "unit", key: "bad"} for key in ("command", "shell", "cwd", "env", "profile")],
])
async def test_untrusted_execution_fields_never_reach_backend(environment, arguments):
    tool, context, artifacts, calls, _ = environment
    result = await tool.execute(arguments, context)
    assert not result.success and result.error == "invalid_arguments"
    assert calls == [] and await artifacts.list_for_run(context.run_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code,passed", [(0, True), (1, False), (2, False), (None, False)])
async def test_exit_code_is_test_outcome_not_protocol_failure(environment, exit_code, passed):
    tool, context, _, _, _ = environment
    tool.executor = LocalSandboxExecutor(lambda request, profile: {"exit_code": exit_code})
    result = await tool.execute({"target": "unit"}, context)
    assert result.success and result.data["passed"] is passed
    assert result.data["exit_code"] == exit_code


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [
    ("rejected", "backend_unavailable"), ("failed", "execution_failed"),
    ("timed_out", "timeout"), ("cancelled", "cancelled"),
])
async def test_sandbox_failures_preserved_in_report(environment, status, code):
    tool, context, artifacts, _, _ = environment

    class Executor:
        async def execute(self, request, profile):
            return SandboxResult(status=status, error_code=code)

    tool.executor = Executor()
    result = await tool.execute({"target": "unit"}, context)
    assert not result.success and result.error == code
    report = json.loads(await artifacts.read(result.data["artifact"]["artifact_id"], 10000))
    assert report["status"] == status and report["passed"] is False


@pytest.mark.asyncio
async def test_real_executor_timeout_cancellation_and_unavailable(environment):
    tool, context, _, calls, _ = environment
    started = asyncio.Event()

    async def slow(request, profile):
        started.set()
        await asyncio.sleep(10)

    tool.executor = LocalSandboxExecutor(slow)
    short = context.model_copy(update={"profile": context.profile.model_copy(
        update={"timeout_seconds": 0.01})})
    result = await tool.execute({"target": "unit"}, short)
    assert not result.success and result.error == "timeout"
    started.clear()
    task = asyncio.create_task(tool.execute({"target": "unit"}, context))
    await started.wait()
    task.cancel()
    result = await task
    assert not result.success and result.error == "cancelled"
    tool.executor = LocalSandboxExecutor()
    result = await tool.execute({"target": "unit"}, context)
    assert not result.success and result.error == "backend_unavailable"
    assert calls == []


@pytest.mark.asyncio
async def test_output_truncation_retains_report_and_status(environment):
    tool, context, artifacts, _, _ = environment
    tool.executor = LocalSandboxExecutor(lambda request, profile: {
        "exit_code": 1, "stdout": "x" * 10000, "stderr": "warning"})
    limited = context.model_copy(update={"profile": context.profile.model_copy(
        update={"max_output_chars": 700})})
    result = await tool.execute({"target": "unit"}, limited)
    assert result.success and result.data["passed"] is False
    assert result.data["output_truncated"] and result.data["truncated"]
    assert len(json.dumps(result.data, ensure_ascii=False)) <= 700
    report = json.loads(await artifacts.read(result.data["artifact"]["artifact_id"], 10000))
    assert report["output_truncated"] and len(report["stdout"]) == 700


@pytest.mark.asyncio
async def test_authority_and_cwd_rejected_before_backend(environment, tmp_path):
    tool, context, _, calls, workspace = environment
    for updates in [
        {"workspace_id": "unknown"}, {"run_id": "unknown"}, {"profile": None},
        {"profile": context.profile.model_copy(update={"allowed_tools": frozenset()})},
        {"profile": context.profile.model_copy(update={"allowed_paths": (str(tmp_path / "other"),)})},
        {"profile": context.profile.model_copy(update={"allow_network": True})},
    ]:
        assert not (await tool.execute({"target": "unit"}, context.model_copy(update=updates))).success
    (workspace.repo_path / "link").symlink_to(tmp_path, target_is_directory=True)
    tool.targets = TestTargetRegistry([RegisteredTestTarget(name="unit", command=("python",), cwd="link")])
    assert not (await tool.execute({"target": "unit"}, context)).success
    assert calls == []


def test_registry_rejects_duplicate_and_unsafe_server_templates():
    target = RegisteredTestTarget(name="unit", command=("python",))
    with pytest.raises(ValueError):
        TestTargetRegistry([target, target])
    for cwd in ("/etc", "../outside"):
        with pytest.raises(ValueError):
            RegisteredTestTarget(name="unit", command=("python",), cwd=cwd)
    with pytest.raises(ValueError):
        RegisteredTestTarget(name="unit", command=())
    registry = TestTargetRegistry([target])
    assert registry.resolve("unit") == target
    with pytest.raises(ValueError):
        registry.resolve("unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_failed", [False, True])
async def test_registry_deadline_is_timeout_even_when_executor_swallows_cancel(environment, cleanup_failed):
    tool, context, artifacts, _, _ = environment

    class Executor:
        async def execute(self, request, profile):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return SandboxResult(status="cancelled", error_code=(
                    "cleanup_failed" if cleanup_failed else "cancelled"))

    tool.executor = Executor()
    result = await ToolRegistry([tool]).execute("run_tests", {"target": "unit"}, context, timeout=0.01)
    code = "cleanup_failed" if cleanup_failed else "timeout"
    assert result.error == code
    assert result.data["status"] == "timed_out"
    report = json.loads(await artifacts.read(result.data["artifact"]["artifact_id"], 10000))
    assert report["status"] == "timed_out" and report["error_code"] == code


@pytest.mark.asyncio
async def test_registry_explicit_cancel_remains_cancelled(environment):
    tool, context, artifacts, _, _ = environment
    started = asyncio.Event()

    class Executor:
        async def execute(self, request, profile):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return SandboxResult(status="cancelled", error_code="cancelled")

    tool.executor = Executor()
    task = asyncio.create_task(ToolRegistry([tool]).execute(
        "run_tests", {"target": "unit"}, context, timeout=10))
    await started.wait()
    task.cancel()
    result = await task
    assert result.error == "cancelled"
    report = json.loads(await artifacts.read(result.data["artifact"]["artifact_id"], 10000))
    assert report["status"] == "cancelled"