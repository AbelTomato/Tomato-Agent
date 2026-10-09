from pathlib import Path

import pytest

from app.agent.policies import CapabilityProfile, CodeTaskCapabilityProfile
from app.agent.sandbox import SandboxRequest
from app.agent.sandbox_docker import DockerSandboxExecutor


IMAGE = "tomato-sandbox@sha256:" + "a" * 64
COMMAND = ["python", "-m", "pytest", "-q", "test_calculator.py"]


def test_docker_command_is_restricted_and_uses_registered_argv(tmp_path: Path):
    executor = DockerSandboxExecutor(IMAGE)
    request = SandboxRequest(
        tool_name="run_tests",
        request_id="request-1",
        arguments={"target": "unit", "argv": COMMAND, "cwd": str(tmp_path)},
    )
    profile = CodeTaskCapabilityProfile(allowed_paths=(str(tmp_path),), max_output_chars=100,
                                        sandbox_backend="docker")

    command = executor.build_run_argv(request, profile, "tomato-agent-request-1")

    assert command[:3] == ["docker", "run", "--rm"]
    assert "--network" in command and command[command.index("--network") + 1] == "none"
    assert "--read-only" in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert "--security-opt" in command
    assert "--user" in command
    assert command[-4:] == COMMAND[1:]
    assert command[command.index("--pull") + 1] == "never"
    assert command[command.index("--memory-swap") + 1] == "256m"
    assert "--mount" in command


@pytest.mark.asyncio
async def test_docker_executor_rejects_cwd_outside_profile(tmp_path: Path):
    executor = DockerSandboxExecutor(IMAGE)
    request = SandboxRequest(
        tool_name="run_tests",
        request_id="request-2",
        arguments={"argv": ["python"], "cwd": str(tmp_path)},
    )

    result = await executor.execute(request, CapabilityProfile(allowed_paths=(str(tmp_path / "other"),)))

    assert result.status == "rejected"
    assert result.error_code == "invalid_request"


@pytest.mark.parametrize("image", ["python:3.12-slim", "", "--privileged", "x@sha256:abc"])
def test_docker_requires_pinned_image(image):
    with pytest.raises(ValueError):
        DockerSandboxExecutor(image)


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    {"argv": ["sh", "-c", "id"]}, {"target": "unknown"}, {"env": {"TOKEN": "secret"}},
    {"cwd": "relative"},
])
async def test_docker_rejects_unregistered_request(tmp_path, updates):
    arguments = {"target": "unit", "argv": COMMAND, "cwd": str(tmp_path)}
    arguments.update(updates)
    request = SandboxRequest(tool_name="run_tests", request_id="request-3", arguments=arguments)
    # A manually copied profile cannot bypass revalidation.
    profile = CodeTaskCapabilityProfile(allowed_paths=(str(tmp_path),)).model_copy(
        update={"sandbox_backend": "docker"})
    result = await DockerSandboxExecutor(IMAGE).execute(request, profile)
    assert result.status == "rejected"


@pytest.mark.asyncio
async def test_missing_backend_authorization_never_starts_process(tmp_path, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("unauthorized request started a process")

    monkeypatch.setattr("asyncio.create_subprocess_exec", forbidden)
    request = SandboxRequest(tool_name="run_tests", request_id="request-4",
                             arguments={"target": "unit", "argv": COMMAND, "cwd": str(tmp_path)})
    result = await DockerSandboxExecutor(IMAGE).execute(
        request, CodeTaskCapabilityProfile(allowed_paths=(str(tmp_path),)))
    assert result.status == "rejected"


@pytest.mark.asyncio
async def test_stream_is_drained_but_retained_output_is_bounded():
    import asyncio

    stream = asyncio.StreamReader()
    stream.feed_data(b"x" * 20000)
    stream.feed_eof()
    output, truncated = await DockerSandboxExecutor._read_bounded(stream, 17)
    assert output == "x" * 17
    assert truncated
    assert stream.at_eof()


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 1])
async def test_cleanup_reports_management_exit_code(monkeypatch, exit_code):
    class Cleanup:
        returncode = exit_code

        async def wait(self):
            return exit_code

    calls = []

    async def spawn(*args, **kwargs):
        calls.append(args)
        return Cleanup()

    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    clean = await DockerSandboxExecutor(IMAGE)._terminate(None, "tomato-agent-owned")
    assert clean is (exit_code == 0)
    assert calls == [("docker", "rm", "-f", "tomato-agent-owned")]


@pytest.mark.asyncio
async def test_cleanup_reports_missing_management_binary(monkeypatch):
    async def spawn(*args, **kwargs):
        raise FileNotFoundError("docker unavailable")

    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    assert not await DockerSandboxExecutor(IMAGE)._terminate(None, "tomato-agent-owned")