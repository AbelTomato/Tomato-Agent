"""Opt-in destructive Docker Desktop restart acceptance for tracked run_tests."""

import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

import aiosqlite
import pytest

from app.agent.policies import CodeTaskCapabilityProfile
from app.agent.sandbox import SandboxRequest
from app.agent.sandbox_docker import DockerSandboxExecutor
from app.execution.docker import DockerTerminationVerifier, TrackedDockerExecutor
from app.execution.models import OperationInput
from app.execution.repository import ExecutionRepository
from app.execution.recovery import RecoveryObservation
from app.execution.worker import ExecutionWorker
from app.tools.base import ToolContext
from test_execution_recovery import setup_repository, snapshot


pytestmark = [
    pytest.mark.skipif(
        os.environ.get("RUN_DOCKER_ACCEPTANCE") != "1",
        reason="explicit real Docker acceptance opt-in required",
    ),
    pytest.mark.skipif(
        os.environ.get("RUN_DOCKER_RESTART_ACCEPTANCE") != "1",
        reason="explicit Docker Desktop restart acceptance opt-in required",
    ),
]


def _windows_docker_cli() -> str:
    configured = os.environ.get("WINDOWS_DOCKER_CLI")
    if configured:
        return configured
    candidates = (
        "/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    pytest.fail("Windows Docker CLI not found; set WINDOWS_DOCKER_CLI to docker.exe")


async def _wait_for_daemon(binary: str, timeout: float, windows_cli: str | None = None) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        process = None
        try:
            if windows_cli:
                process = await asyncio.create_subprocess_exec(
                    windows_cli, "--host", "npipe:////./pipe/docker_engine", "info",
                    "--format", "{{.ID}}",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
            else:
                process = await asyncio.create_subprocess_exec(
                    binary, "--host", "unix:///var/run/docker.sock", "info",
                    "--format", "{{.ID}}",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
            output, _ = await asyncio.wait_for(process.communicate(), timeout=7)
        except TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
                try:
                    await process.wait()
                except OSError:
                    pass
        except OSError:
            pass
        else:
            daemon = output.decode(errors="replace").strip()
            if process.returncode == 0 and daemon:
                return daemon
        await asyncio.sleep(2)
    raise AssertionError("Docker Desktop daemon did not recover before deadline")


async def test_desktop_restart_during_tracked_run_tests_keeps_unclosed_launch_quarantined(tmp_path):
    binary = shutil.which("docker")
    assert binary, "Docker CLI is required"
    image = os.environ["SANDBOX_IMAGE"]
    windows_cli = _windows_docker_cli()
    await _wait_for_daemon(binary, 10, windows_cli)

    workspace = tmp_path / "restart-workspace"
    workspace.mkdir()
    workspace.chmod(0o755)
    (workspace / "test_restart.py").write_text(
        "import time\n\ndef test_wait_for_restart():\n    time.sleep(180)\n"
    )
    (workspace / "test_restart.py").chmod(0o644)

    repository, run, lease, clock = await setup_repository(tmp_path)
    operation_id = uuid4()
    operation = OperationInput(
        id=operation_id,
        logical_index=0,
        kind="tool",
        tool_name="run_tests",
        input_payload={"target": "unit"},
        recovery_class="sandbox",
    )
    saved = snapshot(run.id).model_copy(update={
        "phase": "ready_model",
        "pending_operation_id": operation_id,
    })
    await repository.prepare_operation(lease, operation, saved)
    step = await repository.start_step(lease, operation_id, saved)
    await repository.commit_checkpoint(lease, saved.model_dump(mode="json"))
    async with aiosqlite.connect(repository.path) as db:
        await db.execute(
            "INSERT INTO assistant_effect_settlements(invocation_id,resolution) VALUES (?,?)",
            (str(operation_id), "executed"),
        )
        await db.commit()
    request_id = f"daemon-restart-{uuid4().hex}"
    request = SandboxRequest(
        tool_name="run_tests",
        request_id=request_id,
        arguments={
            "target": "unit",
            "argv": ["python", "-m", "pytest", "-q", "test_restart.py"],
            "cwd": str(workspace),
        },
    )
    profile = CodeTaskCapabilityProfile(
        sandbox_backend="docker",
        allowed_paths=(str(workspace),),
        timeout_seconds=240,
        max_output_chars=1000,
    )
    context = ToolContext(
        session_id="docker-restart-acceptance",
        run_id=str(run.id),
        invocation_id=str(operation_id),
        step_id=str(step.id),
        lease=lease,
    )
    executor = DockerSandboxExecutor(
        image,
        registered_targets={str(workspace): {
            "unit": ("python", "-m", "pytest", "-q", "test_restart.py"),
        }},
    )
    tracked = TrackedDockerExecutor(executor, repository, context)
    execution = asyncio.create_task(tracked.execute(request, profile))
    container_name = None
    restart_process = None
    report = {"run_id": str(run.id), "invocation_id": str(operation_id), "step_id": str(step.id)}
    try:
        for _ in range(120):
            async with aiosqlite.connect(repository.path) as db:
                row = await (await db.execute(
                    "SELECT container_name,daemon_id,launch_closed,lifecycle_state "
                    "FROM docker_tool_executions WHERE step_id=?", (str(step.id),)
                )).fetchone()
            if row:
                container_name = row[0]
                report["identity_before_restart"] = {
                    "container_name": row[0], "daemon_id": row[1],
                    "launch_closed": row[2], "lifecycle_state": row[3],
                }
                probe = await asyncio.create_subprocess_exec(
                    binary, "--host", "unix:///var/run/docker.sock", "inspect", "--format",
                    "{{.State.Running}}", container_name,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                output, _ = await asyncio.wait_for(probe.communicate(), timeout=8)
                if probe.returncode == 0 and output.strip() == b"true":
                    break
            if execution.done():
                pytest.fail(f"tracked run_tests exited before Docker Desktop restart: {execution.result()}")
            await asyncio.sleep(0.25)
        else:
            pytest.fail("tracked Docker container did not become visible")

        restart_process = await asyncio.create_subprocess_exec(
            windows_cli, "desktop", "restart",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            restart_output, _ = await asyncio.wait_for(restart_process.communicate(), timeout=90)
        except TimeoutError:
            restart_process.kill()
            await restart_process.wait()
            raise AssertionError("Docker Desktop restart command exceeded 90 seconds")
        report["restart_output"] = restart_output.decode(errors="replace")[-4000:]
        assert restart_process.returncode == 0, report["restart_output"]

        # During disruption, no settlement path may manufacture evidence.
        async with aiosqlite.connect(repository.path) as db:
            during = await (await db.execute(
                "SELECT termination_evidence FROM docker_tool_executions WHERE step_id=?",
                (str(step.id),),
            )).fetchone()
        assert during == (None,)

        recovered_daemon = await _wait_for_daemon(binary, 120, windows_cli)
        report["daemon_after_restart"] = recovered_daemon
        result = await asyncio.wait_for(execution, timeout=30)
        report["executor_result"] = result.model_dump(mode="json")
        assert result.status != "completed", "long-running test unexpectedly completed before restart"

        # Current policy deliberately refuses evidence for an unclosed launch,
        # even when a post-restart query might observe the container as absent.
        clock.value += timedelta(seconds=31)
        repository = ExecutionRepository(repository.path, clock=clock)
        worker = ExecutionWorker(
            repository,
            None,
            recovery_observation_factory=lambda _: RecoveryObservation(policy_digest="policy"),
        )
        worker.docker_recovery_verifier = DockerTerminationVerifier(repository, binary, revoke_unclosed=True)
        recovery = await asyncio.wait_for(worker.recover_on_startup(), timeout=45)
        report["recovery"] = [{"status": item.status, "action": item.action,
                                "reason_code": item.reason_code} for item in recovery]
        async with aiosqlite.connect(repository.path) as db:
            final_row = await (await db.execute(
                "SELECT daemon_id,launch_closed,lifecycle_state,revocation_attempts,termination_evidence "
                "FROM docker_tool_executions WHERE step_id=?", (str(step.id),)
            )).fetchone()
        report["execution_after_recovery"] = {
            "daemon_id": final_row[0],
            "launch_closed": final_row[1],
            "lifecycle_state": final_row[2],
            "revocation_attempts": final_row[3],
            "termination_evidence": final_row[4],
        }
        assert final_row[0] == report["identity_before_restart"]["daemon_id"]
        assert final_row[1] == 0
        assert final_row[4] is None
        assert final_row[2] == "unknown"
        assert recovery and recovery[0].action == "manual_review"
        async with aiosqlite.connect(repository.path) as db:
            operation_row = await (await db.execute(
                "SELECT status FROM execution_operations WHERE id=?", (str(operation_id),)
            )).fetchone()
        assert operation_row == ("unknown",)
        projection = (await repository.list_effect_settlements(run.id))[0]
        report["settlement"] = {
            "quarantined": projection["quarantined"],
            "termination_evidence": projection["termination_evidence"],
        }
        assert projection["quarantined"]
        assert projection["termination_evidence"] is None
        (tmp_path / "docker-daemon-restart-report.json").write_text(
            json.dumps(report, indent=2)
        )
    finally:
        if restart_process is not None and restart_process.returncode is None:
            restart_process.kill()
            await restart_process.wait()
        try:
            await _wait_for_daemon(binary, 120, windows_cli)
        except AssertionError:
            pass
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        (tmp_path / "docker-daemon-restart-report.json").write_text(json.dumps(report, indent=2))
        if container_name:
            cleanup = await asyncio.create_subprocess_exec(
                binary, "--host", "unix:///var/run/docker.sock", "rm", "-f", container_name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(cleanup.wait(), timeout=15)
            except TimeoutError:
                cleanup.kill()
                await cleanup.wait()