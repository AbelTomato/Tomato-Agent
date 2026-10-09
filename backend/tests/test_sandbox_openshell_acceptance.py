"""Independent OpenShell gate. Opt-in prerequisites must fail, not skip."""

import asyncio
import os
import subprocess

import pytest

from app.agent.policies import CodeTaskCapabilityProfile
from app.agent.sandbox import SandboxRequest
from app.agent.sandbox_openshell import OpenShellSandboxExecutor


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_OPENSHELL_ACCEPTANCE") != "1",
    reason="explicit OpenShell acceptance opt-in required",
)


@pytest.mark.parametrize("scenario", ["isolation", "timeout", "cancelled"])
async def test_real_openshell_gate(tmp_path, monkeypatch, scenario):
    binary = os.environ.get("CODE_TASK_OPENSHELL_BINARY", "openshell")
    image = os.environ["CODE_TASK_OPENSHELL_IMAGE"]
    subprocess.run([binary, "--version"], check=True, capture_output=True, timeout=10)
    sources = {
        "isolation": '''import os, socket
from pathlib import Path
import pytest
def test_isolation():
    assert os.getuid() != 0
    assert "SANDBOX_HOST_SECRET" not in os.environ
    assert not Path("/var/run/docker.sock").exists()
    with pytest.raises(OSError):
        Path("/etc/forbidden").write_text("escape")
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 443), timeout=1)
''',
        "timeout": "import time\ndef test_sleep():\n    time.sleep(60)\n",
        "cancelled": "import time\ndef test_sleep():\n    time.sleep(60)\n",
    }
    (tmp_path / "test_calculator.py").write_text(sources[scenario])
    monkeypatch.setenv("SANDBOX_HOST_SECRET", "acceptance-sentinel")
    executor = OpenShellSandboxExecutor(image, openshell_binary=binary)
    original_run = executor._run
    executing = asyncio.Event()
    names = []

    async def observe(command, *args):
        if command[2] == "create":
            names.append(command[4])
        if command[2] == "exec":
            executing.set()
        return await original_run(command, *args)

    monkeypatch.setattr(executor, "_run", observe)
    request = SandboxRequest(tool_name="run_tests", request_id="acceptance", arguments={
        "target": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calculator.py"],
        "cwd": str(tmp_path),
    })
    profile = CodeTaskCapabilityProfile(sandbox_backend="openshell",
        allowed_paths=(str(tmp_path),), timeout_seconds=30 if scenario == "timeout" else 60)
    task = asyncio.create_task(executor.execute(request, profile))
    if scenario == "cancelled":
        try:
            await asyncio.wait_for(executing.wait(), 55)
        finally:
            task.cancel()
    result = await asyncio.wait_for(task, 85)
    assert result.error_code != "cleanup_failed", result
    assert result.status == {"isolation": "completed", "timeout": "timed_out",
                             "cancelled": "cancelled"}[scenario], result
    if scenario == "isolation":
        assert result.exit_code == 0, result
    assert names
    assert not await executor._owned(names[0]), "owned sandbox remains"