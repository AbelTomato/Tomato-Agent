"""OpenShell CLI-backed sandbox executor.

This adapter uses only the documented OpenShell CLI surface: sandbox create,
upload, exec, and delete. The CLI is expected to be pre-installed and
authenticated by the service owner.
"""

import asyncio
import contextlib
import json
import os
import re
import tempfile
from pathlib import Path
from uuid import uuid4

from app.agent.policies import CapabilityProfile, CodeTaskCapabilityProfile, validate_capability_profile

from .sandbox import SandboxRequest, SandboxResult


class OpenShellSandboxExecutor:
    def __init__(self, image: str, *, openshell_binary: str = "openshell") -> None:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:\-]*@sha256:[0-9a-f]{64}", image):
            raise ValueError("OpenShell image must be pinned with a sha256 digest")
        self.image = image
        self.openshell_binary = openshell_binary

    @staticmethod
    def _policy() -> str:
        return """version: 1
filesystem_policy:
  include_workdir: false
  read_only: [/bin, /usr, /lib, /proc, /dev/urandom, /etc]
  read_write: [/tmp/tomato-workspace, /tmp, /dev/null]
landlock:
  compatibility: hard_requirement
network_policies: {}
"""

    def build_create_argv(self, name: str, policy_path: str) -> list[str]:
        return [self.openshell_binary, "sandbox", "create", "--name", name,
                "--from", self.image, "--policy", policy_path, "--detach",
                "--no-auto-providers", "--cpu", "1", "--memory", "256Mi",
                "--label", f"tomato-agent.owner={name}", "--output", "json",
                "--", "/bin/sleep", "infinity"]

    def build_upload_argv(self, name: str, workspace: str) -> list[str]:
        return [self.openshell_binary, "sandbox", "upload", name, workspace,
                "/tmp/tomato-workspace", "--no-git-ignore"]

    def build_exec_argv(self, name: str, timeout: float, workspace_name: str = "") -> list[str]:
        return [self.openshell_binary, "sandbox", "exec", "--name", name,
                "--workdir", str(Path("/tmp/tomato-workspace") / workspace_name),
                "--timeout", str(max(1, int(timeout))),
                "--no-tty", "--no-login-shell", "--env", "BASH_ENV=/dev/null",
                "--", "python", "-m",
                "pytest", "-q", "test_calculator.py"]

    def build_delete_argv(self, name: str) -> list[str]:
        return [self.openshell_binary, "sandbox", "delete", name]

    async def execute(self, request: SandboxRequest, profile: CapabilityProfile) -> SandboxResult:
        name = f"ta-{uuid4().hex[:16]}"
        policy_path: str | None = None
        attempted = False
        result = SandboxResult(status="failed", error_code="execution_failed")
        try:
            if not isinstance(profile, CodeTaskCapabilityProfile):
                raise ValueError("code task authorization required")
            validate_capability_profile(profile)
            profile = CodeTaskCapabilityProfile.model_validate(profile.model_dump())
            args = request.arguments
            if (profile.sandbox_backend != "openshell" or "run_tests" not in profile.allowed_tools
                    or request.tool_name != "run_tests" or set(args) != {"target", "argv", "cwd"}
                    or args["target"] != "unit"
                    or args["argv"] != ["python", "-m", "pytest", "-q", "test_calculator.py"]):
                raise ValueError("OpenShell execution is not authorized")
            cwd = args["cwd"]
            if not isinstance(cwd, str) or not Path(cwd).is_absolute():
                raise ValueError("absolute workspace required")
            workspace = Path(cwd).resolve()
            allowed = tuple(Path(path).resolve() for path in profile.allowed_paths)
            if not workspace.is_dir() or not any(
                workspace == root or workspace.is_relative_to(root) for root in allowed
            ):
                raise ValueError("sandbox cwd is outside the capability profile")
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as policy:
                policy.write(self._policy())
                policy_path = policy.name
            deadline = asyncio.get_running_loop().time() + profile.timeout_seconds
            for phase, command in (
                ("create", self.build_create_argv(name, policy_path)),
                ("upload", self.build_upload_argv(name, str(workspace))),
                ("exec", self.build_exec_argv(name, profile.timeout_seconds, workspace.name)),
            ):
                if phase == "create":
                    attempted = True
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                stdout, stderr, exit_code, truncated = await self._run(
                    command, remaining, profile.max_output_chars
                )
                if phase != "exec" and exit_code != 0:
                    result = SandboxResult(
                        status="failed",
                        error_code="backend_unavailable" if exit_code in (125, 126, 127) else "execution_failed",
                        stdout=stdout,
                        stderr=stderr,
                        output_truncated=truncated,
                    )
                    return result
                if phase == "exec":
                    result = SandboxResult(
                        status="completed" if exit_code in range(6) else "failed",
                        exit_code=exit_code,
                        stdout=stdout,
                        stderr=stderr,
                        output_truncated=truncated,
                        error_code=None if exit_code in range(6) else "execution_failed",
                    )
                    return result
            raise AssertionError("OpenShell command sequence did not execute")
        except asyncio.TimeoutError:
            result = SandboxResult(status="timed_out", error_code="timeout")
            return result
        except asyncio.CancelledError:
            result = SandboxResult(status="cancelled", error_code="cancelled")
            return result
        except (OSError, FileNotFoundError):
            result = SandboxResult(status="failed", error_code="backend_unavailable")
            return result
        except (AttributeError, TypeError, ValueError):
            result = SandboxResult(status="rejected", error_code="invalid_request")
            return result
        except Exception:
            return result
        finally:
            if policy_path:
                with contextlib.suppress(OSError):
                    os.unlink(policy_path)
            if attempted:
                cleanup = asyncio.create_task(self._cleanup(name, 10))
                cleanup_ok = await self._drain(cleanup)
                if not cleanup_ok:
                    if result.status == "completed":
                        result.status = "failed"
                    result.error_code = "cleanup_failed"

    async def _run(self, command: list[str], timeout: float, limit: int) -> tuple[str, str, int, bool]:
        launch = asyncio.create_task(asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env={key: os.environ[key] for key in ("PATH", "HOME", "XDG_CONFIG_HOME") if key in os.environ},
        ))
        process = None
        try:
            async with asyncio.timeout(timeout):
                process = await asyncio.shield(launch)
                out, err, code = await asyncio.gather(
                    self._read(process.stdout, limit),
                    self._read(process.stderr, limit),
                    process.wait(),
                )
            stdout = out[0][:limit]
            stderr = err[0][:max(0, limit - len(stdout))]
            return stdout, stderr, code, out[1] or err[1] or len(out[0]) + len(err[0]) > limit
        except (asyncio.TimeoutError, asyncio.CancelledError):
            process = await self._drain(launch)
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(OSError, asyncio.TimeoutError):
                await self._drain(asyncio.create_task(asyncio.wait_for(process.wait(), 5)))
            raise

    async def _cleanup(self, name: str, timeout: float) -> bool:
        try:
            async def remove_owned():
                owned = await self._owned(name)
                if owned:
                    result = await self._run(self.build_delete_argv(name), timeout, 1000)
                    if result[2] != 0:
                        return False
                # A failed create may have committed server-side. Always observe
                # the owner selector; never interpret transport failure as absence.
                while await self._owned(name):
                    await asyncio.sleep(0.1)
                return True

            return await asyncio.wait_for(remove_owned(), timeout)
        except Exception:
            return False

    async def _owned(self, name: str) -> bool:
        stdout, _, code, truncated = await self._run(
            [self.openshell_binary, "sandbox", "list", "--selector",
             f"tomato-agent.owner={name}", "--output", "json"], 5, 10000
        )
        if code or truncated:
            raise RuntimeError("sandbox ownership could not be verified")
        data = json.loads(stdout)
        items = data["sandboxes"]
        if not isinstance(items, list) or data.get("next_page_token"):
            raise ValueError("invalid sandbox list")
        for item in items:
            if item.get("name") != name or item.get("labels", {}).get("tomato-agent.owner") != name:
                raise ValueError("sandbox ownership mismatch")
        return bool(items)

    @staticmethod
    async def _drain(task):
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    return task.result()

    @staticmethod
    async def _read(stream, limit: int) -> tuple[str, bool]:
        if stream is None:
            return "", False
        data = bytearray()
        truncated = False
        while True:
            chunk = await stream.read(min(8192, limit + 1))
            if not chunk:
                break
            remaining = limit - len(data)
            data.extend(chunk[:max(remaining, 0)])
            truncated |= len(chunk) > max(remaining, 0)
        return data.decode("utf-8", errors="replace"), truncated
