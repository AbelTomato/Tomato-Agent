"""Docker-backed sandbox executor.

The Docker CLI is deliberately used through argv, never through a shell.  The
server owns the image, mounts, capabilities, and resource limits; a sandbox
request can only supply the registered argv and workspace path.
"""

import asyncio
import contextlib
import os
import re
from pathlib import Path
from uuid import uuid4

from app.agent.policies import CapabilityProfile, CodeTaskCapabilityProfile, validate_capability_profile

from .sandbox import SandboxRequest, SandboxResult


_CONTAINER_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")


class DockerSandboxExecutor:
    """Execute one registered command in a short-lived, restricted container."""

    def __init__(self, image: str, *, docker_binary: str = "docker") -> None:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:\-]*@sha256:[0-9a-f]{64}", image):
            raise ValueError("image must be pinned with a sha256 digest")
        self.image = image
        self.docker_binary = docker_binary

    def build_run_argv(
        self, request: SandboxRequest, profile: CapabilityProfile, container_name: str
    ) -> list[str]:
        if not _CONTAINER_NAME.fullmatch(container_name):
            raise ValueError("invalid container name")
        if not isinstance(profile, CodeTaskCapabilityProfile):
            raise ValueError("code task authorization required")
        profile = CodeTaskCapabilityProfile.model_validate(profile.model_dump())
        if profile.sandbox_backend != "docker" or "run_tests" not in profile.allowed_tools:
            raise ValueError("Docker execution is not authorized")
        if request.tool_name != "run_tests":
            raise ValueError("only registered tests may execute")
        arguments = request.arguments
        argv = arguments.get("argv")
        cwd = arguments.get("cwd")
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(item, str) or not item or "\x00" in item for item in argv)
            or not isinstance(cwd, str)
        ):
            raise ValueError("sandbox request must contain a valid argv and cwd")
        if (set(arguments) != {"target", "argv", "cwd"}
                or arguments["target"] != "unit"
                or argv != ["python", "-m", "pytest", "-q", "test_calculator.py"]):
            raise ValueError("unregistered test command")
        if not Path(cwd).is_absolute() or any(c in cwd for c in (",", ":", "\n")):
            raise ValueError("invalid mount source")
        workspace = Path(cwd).resolve()
        if not workspace.is_dir():
            raise ValueError("sandbox cwd must be an existing directory")
        allowed = tuple(Path(path).resolve() for path in profile.allowed_paths)
        if not any(workspace == root or workspace.is_relative_to(root) for root in allowed):
            raise ValueError("sandbox cwd is outside the capability profile")

        return [
            self.docker_binary,
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            container_name,
            "--label",
            f"tomato-agent.request_id={request.request_id}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "64",
            "--memory",
            "256m",
            "--memory-swap",
            "256m",
            "--cpus",
            "1.0",
            "--user",
            "65532:65532",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--log-driver",
            "none",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTEST_ADDOPTS=-p no:cacheprovider",
            "--env",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
            "--entrypoint",
            "python",
            "--mount",
            f"type=bind,src={workspace},dst=/workspace,readonly",
            "-w",
            "/workspace",
            self.image,
            *argv[1:],
        ]

    async def execute(
        self, request: SandboxRequest, profile: CapabilityProfile
    ) -> SandboxResult:
        try:
            validate_capability_profile(profile)
            container_name = f"tomato-agent-{uuid4().hex}"
            command = self.build_run_argv(request, profile, container_name)
        except (AttributeError, TypeError, ValueError):
            return SandboxResult(status="rejected", error_code="invalid_request")

        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"PATH": os.environ.get("PATH", "")},
            )
            stdout, stderr, exit_code = await asyncio.wait_for(
                asyncio.gather(
                    self._read_bounded(process.stdout, profile.max_output_chars),
                    self._read_bounded(process.stderr, profile.max_output_chars),
                    process.wait(),
                ),
                profile.timeout_seconds,
            )
            output, output_truncated = self._merge_output(stdout[0], stderr[0], profile.max_output_chars)
            if exit_code in (125, 126, 127):
                return SandboxResult(status="failed", error_code="backend_unavailable")
            return SandboxResult(
                status="completed",
                exit_code=exit_code,
                stdout=output[0],
                stderr=output[1],
                output_truncated=output_truncated or stdout[1] or stderr[1],
            )
        except asyncio.TimeoutError:
            clean = await self._terminate(process, container_name)
            return SandboxResult(status="timed_out", error_code="timeout" if clean else "cleanup_failed")
        except asyncio.CancelledError:
            clean = await self._terminate(process, container_name)
            return SandboxResult(status="cancelled", error_code="cancelled" if clean else "cleanup_failed")
        except (OSError, FileNotFoundError):
            if process is not None and not await self._terminate(process, container_name):
                return SandboxResult(status="failed", error_code="cleanup_failed")
            return SandboxResult(status="failed", error_code="backend_unavailable")
        except Exception:
            if process is not None and not await self._terminate(process, container_name):
                return SandboxResult(status="failed", error_code="cleanup_failed")
            return SandboxResult(status="failed", error_code="execution_failed")

    @staticmethod
    async def _read_bounded(stream: asyncio.StreamReader | None, limit: int) -> tuple[str, bool]:
        if stream is None:
            return "", False
        data = bytearray()
        truncated = False
        while True:
            chunk = await stream.read(min(8192, limit + 1))
            if not chunk:
                break
            remaining = limit - len(data)
            if remaining > 0:
                data.extend(chunk[:remaining])
            if len(chunk) > max(remaining, 0):
                truncated = True
        return data.decode("utf-8", errors="replace"), truncated

    @staticmethod
    def _merge_output(stdout: str, stderr: str, limit: int) -> tuple[tuple[str, str], bool]:
        if len(stdout) + len(stderr) <= limit:
            return (stdout, stderr), False
        return (stdout[:limit], ""), True

    async def _terminate(self, process: asyncio.subprocess.Process | None, name: str) -> bool:
        clean = True
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except (OSError, asyncio.TimeoutError):
                clean = False
        cleanup = None
        try:
            cleanup = await asyncio.wait_for(asyncio.create_subprocess_exec(
                self.docker_binary, "rm", "-f", name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env={"PATH": os.environ.get("PATH", "")},
            ), 5)
            return await asyncio.wait_for(cleanup.wait(), 5) == 0 and clean
        except (OSError, asyncio.TimeoutError):
            if cleanup is not None and cleanup.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    cleanup.kill()
                with contextlib.suppress(OSError, asyncio.TimeoutError):
                    await asyncio.wait_for(cleanup.wait(), 5)
            return False