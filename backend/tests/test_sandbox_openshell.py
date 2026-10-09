import asyncio
from pathlib import Path

import pytest

from app.agent.policies import CodeTaskCapabilityProfile
from app.agent.sandbox import SandboxRequest
from app.agent.sandbox_openshell import OpenShellSandboxExecutor


IMAGE = "registry.example.test/python-pytest@sha256:" + "a" * 64
COMMAND = ["python", "-m", "pytest", "-q", "test_calculator.py"]


def test_openshell_uses_documented_lifecycle_and_fixed_command(tmp_path: Path):
    executor = OpenShellSandboxExecutor(IMAGE, openshell_binary="openshell-test")

    assert executor.build_create_argv("sandbox-1", "/tmp/policy.yaml") == [
        "openshell-test", "sandbox", "create", "--name", "sandbox-1",
        "--from", IMAGE, "--policy", "/tmp/policy.yaml", "--detach",
        "--no-auto-providers", "--cpu", "1", "--memory", "256Mi",
        "--label", "tomato-agent.owner=sandbox-1", "--output", "json",
        "--", "/bin/sleep", "infinity",
    ]
    assert executor.build_upload_argv("sandbox-1", str(tmp_path))[-3:] == [str(tmp_path), "/tmp/tomato-workspace", "--no-git-ignore"]
    assert executor.build_exec_argv("sandbox-1", 20)[-6:] == ["--", *COMMAND]
    command = executor.build_exec_argv("sandbox-1", 20, "source-directory")
    assert command[command.index("--workdir") + 1] == "/tmp/tomato-workspace/source-directory"
    assert executor.build_delete_argv("sandbox-1") == ["openshell-test", "sandbox", "delete", "sandbox-1"]


@pytest.mark.asyncio
async def test_openshell_rejects_unauthorized_request_without_starting_process(tmp_path, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("unauthorized request started OpenShell")

    monkeypatch.setattr("asyncio.create_subprocess_exec", forbidden)
    request = SandboxRequest(tool_name="run_tests", request_id="openshell-1", arguments={
        "target": "unit", "argv": COMMAND, "cwd": str(tmp_path),
    })
    profile = CodeTaskCapabilityProfile(allowed_paths=(str(tmp_path),))
    result = await OpenShellSandboxExecutor(IMAGE).execute(request, profile)
    assert result.status == "rejected"


@pytest.mark.asyncio
async def test_openshell_cleanup_failure_is_reported(tmp_path, monkeypatch):
    calls = []

    class Process:
        returncode = 0

        def __init__(self, output=b""):
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(output)
            self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()

        async def wait(self):
            return 0

    async def spawn(*args, **kwargs):
        calls.append(args)
        if args[2] == "create":
            assert len(args[4]) <= 19
            assert args[4].startswith("ta-")
            return Process()
        if args[2] == "upload":
            return Process()
        if args[2] == "exec":
            return Process()
        if args[2] == "list":
            import json
            name = args[4].split("=", 1)[1]
            return Process(json.dumps({"sandboxes": [{"name": name,
                "labels": {"tomato-agent.owner": name}}]}).encode())
        raise OSError("delete unavailable")

    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "test_calculator.py").write_text("def test_ok(): pass\n")
    request = SandboxRequest(tool_name="run_tests", request_id="openshell-2", arguments={
        "target": "unit", "argv": COMMAND, "cwd": str(workspace),
    })
    profile = CodeTaskCapabilityProfile(
        allowed_paths=(str(tmp_path),), sandbox_backend="openshell", timeout_seconds=2,
    )
    result = await OpenShellSandboxExecutor(IMAGE).execute(request, profile)
    assert result.status == "failed"
    assert result.error_code == "cleanup_failed"
    assert [args[1:3] for args in calls] == [
        ("sandbox", "create"), ("sandbox", "upload"),
        ("sandbox", "exec"), ("sandbox", "list"), ("sandbox", "delete"),
    ]


@pytest.mark.parametrize("image", ["", "python:latest", "/tmp/rootfs.tar"])
def test_unpinned_images_are_rejected(image):
    with pytest.raises(ValueError):
        OpenShellSandboxExecutor(image)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["create", "upload", "exec"])
@pytest.mark.parametrize("fault", ["timeout", "cancel", "disconnect"])
async def test_partial_lifecycle_always_checks_cleanup(tmp_path, monkeypatch, phase, fault):
    executor = OpenShellSandboxExecutor(IMAGE)
    calls = []

    async def run(command, timeout, limit):
        calls.append(command[2])
        if command[2] == phase:
            raise {"timeout": asyncio.TimeoutError, "cancel": asyncio.CancelledError,
                   "disconnect": OSError}[fault]()
        return "", "", 0, False

    cleaned = []

    async def cleanup(name, timeout):
        cleaned.append(name)
        return True

    monkeypatch.setattr(executor, "_run", run)
    monkeypatch.setattr(executor, "_cleanup", cleanup)
    request = SandboxRequest(tool_name="run_tests", request_id="fault", arguments={
        "target": "unit", "argv": COMMAND, "cwd": str(tmp_path),
    })
    result = await executor.execute(request, CodeTaskCapabilityProfile(
        sandbox_backend="openshell", allowed_paths=(str(tmp_path),)))
    assert result.status == {"timeout": "timed_out", "cancel": "cancelled",
                             "disconnect": "failed"}[fault]
    assert len(cleaned) == 1


@pytest.mark.asyncio
async def test_repeated_cancellation_does_not_interrupt_cleanup(tmp_path, monkeypatch):
    executor = OpenShellSandboxExecutor(IMAGE)
    started = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()

    async def run(*args):
        started.set()
        await asyncio.Event().wait()

    async def cleanup(*args):
        cleaning.set()
        await release.wait()
        return True

    monkeypatch.setattr(executor, "_run", run)
    monkeypatch.setattr(executor, "_cleanup", cleanup)
    request = SandboxRequest(tool_name="run_tests", request_id="cancel", arguments={
        "target": "unit", "argv": COMMAND, "cwd": str(tmp_path),
    })
    task = asyncio.create_task(executor.execute(request, CodeTaskCapabilityProfile(
        sandbox_backend="openshell", allowed_paths=(str(tmp_path),))))
    await started.wait()
    task.cancel()
    await cleaning.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    result = await asyncio.wait_for(task, 1)
    assert result.status == "cancelled"
    assert result.error_code == "cancelled"


@pytest.mark.asyncio
async def test_cleanup_waits_for_pending_deletion_and_rejects_daemon_loss(monkeypatch):
    executor = OpenShellSandboxExecutor(IMAGE)
    states = iter([True, True, False])

    async def owned(name):
        return next(states)

    async def run(*args):
        return "cleanup pending", "", 0, False

    monkeypatch.setattr(executor, "_owned", owned)
    monkeypatch.setattr(executor, "_run", run)
    assert await executor._cleanup("owned", 1)

    async def disconnected(name):
        raise OSError

    monkeypatch.setattr(executor, "_owned", disconnected)
    assert not await executor._cleanup("owned", 1)


@pytest.mark.asyncio
async def test_subprocess_output_uses_combined_budget(monkeypatch):
    class Process:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            for stream in (self.stdout, self.stderr):
                stream.feed_data(b"abcdefgh")
                stream.feed_eof()

        async def wait(self):
            return 1

    async def spawn(*args, **kwargs):
        assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
        assert "SANDBOX_HOST_SECRET" not in kwargs["env"]
        return Process()

    monkeypatch.setenv("SANDBOX_HOST_SECRET", "sentinel")
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    result = await OpenShellSandboxExecutor(IMAGE)._run(["openshell"], 1, 10)
    assert result == ("abcdefgh", "ab", 1, True)


@pytest.mark.asyncio
async def test_cancellation_during_spawn_reaps_late_process(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    killed = asyncio.Event()

    class Process:
        def kill(self):
            killed.set()

        async def wait(self):
            return -9

    async def spawn(*args, **kwargs):
        started.set()
        await release.wait()
        return Process()

    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    executor = OpenShellSandboxExecutor(IMAGE)
    task = asyncio.create_task(executor._run(["openshell"], 1, 10))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert killed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code,status", [(0, "completed"), (1, "completed"),
                                              (5, "completed"), (127, "failed")])
async def test_test_exit_codes_are_preserved(tmp_path, monkeypatch, exit_code, status):
    executor = OpenShellSandboxExecutor(IMAGE)

    async def run(command, *args):
        if command[2] == "exec":
            assert command[command.index("--workdir") + 1] == f"/tmp/tomato-workspace/{tmp_path.name}"
        return "", "", exit_code if command[2] == "exec" else 0, False

    async def cleanup(*args):
        return True

    monkeypatch.setattr(executor, "_run", run)
    monkeypatch.setattr(executor, "_cleanup", cleanup)
    request = SandboxRequest(tool_name="run_tests", request_id="exit", arguments={
        "target": "unit", "argv": COMMAND, "cwd": str(tmp_path),
    })
    result = await executor.execute(request, CodeTaskCapabilityProfile(
        sandbox_backend="openshell", allowed_paths=(str(tmp_path),)))
    assert result.status == status
    assert result.exit_code == exit_code


@pytest.mark.asyncio
async def test_cleanup_never_deletes_a_mismatched_owner(monkeypatch):
    executor = OpenShellSandboxExecutor(IMAGE)
    calls = []

    async def run(command, *args):
        calls.append(command[2])
        return '{"sandboxes": [{"name": "someone-else", "labels": {}}]}', "", 0, False

    monkeypatch.setattr(executor, "_run", run)
    assert not await executor._cleanup("owned", 1)
    assert calls == ["list"]
