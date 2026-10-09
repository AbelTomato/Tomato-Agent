"""Opt-in real Docker gates; missing prerequisites fail, never count as passes."""

import asyncio
import os
import subprocess

import pytest

from app.agent.policies import CodeTaskCapabilityProfile
from app.agent.sandbox import SandboxRequest
from app.agent.sandbox_docker import DockerSandboxExecutor


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DOCKER_ACCEPTANCE") != "1", reason="explicit Docker acceptance opt-in required"
)


@pytest.mark.parametrize("scenario", ["isolation", "timeout", "cancelled"])
async def test_real_docker_gate(tmp_path, monkeypatch, scenario):
    image = os.environ["SANDBOX_IMAGE"]
    subprocess.run(["docker", "version"], check=True, capture_output=True, timeout=10)
    tmp_path.chmod(0o755)
    sources = {
        "isolation": '''import os, socket
from pathlib import Path
import pytest
def test_isolation():
    assert os.getuid() == 65532
    assert "SANDBOX_HOST_SECRET" not in os.environ
    assert not Path("/var/run/docker.sock").exists()
    assert not Path("/host-secret").exists()
    for path in ("/workspace/forbidden", "/forbidden"):
        with pytest.raises(OSError):
            Path(path).write_text("escape")
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 443), timeout=1)
    status = Path("/proc/self/status").read_text()
    assert "CapEff:\\t0000000000000000" in status
    assert "NoNewPrivs:\\t1" in status
''',
        "timeout": "import time\ndef test_sleep():\n    time.sleep(60)\n",
        "cancelled": "import time\ndef test_sleep():\n    time.sleep(60)\n",
    }
    source = tmp_path / "test_calculator.py"
    source.write_text(sources[scenario])
    source.chmod(0o644)
    monkeypatch.setenv("SANDBOX_HOST_SECRET", "acceptance-sentinel-not-a-credential")
    request_id = f"acceptance-{scenario}"
    request = SandboxRequest(tool_name="run_tests", request_id=request_id, arguments={
        "target": "unit", "argv": ["python", "-m", "pytest", "-q", "test_calculator.py"],
        "cwd": str(tmp_path),
    })
    profile = CodeTaskCapabilityProfile(sandbox_backend="docker", allowed_paths=(str(tmp_path),),
                                        timeout_seconds=2 if scenario == "timeout" else 20,
                                        max_output_chars=1000)
    task = asyncio.create_task(DockerSandboxExecutor(image).execute(request, profile))
    if scenario == "cancelled":
        for _ in range(100):
            running = await asyncio.to_thread(subprocess.run, ["docker", "ps", "-q", "--filter",
                f"label=tomato-agent.request_id={request_id}"], check=True, capture_output=True, timeout=5)
            if running.stdout.strip():
                break
            await asyncio.sleep(0.05)
        else:
            await task
            pytest.fail("container did not start before cancellation gate")
        task.cancel()
    result = await asyncio.wait_for(task, 40)
    if scenario in ("timeout", "cancelled"):
        assert result.status == ("timed_out" if scenario == "timeout" else "cancelled")
        assert result.error_code == ("timeout" if scenario == "timeout" else "cancelled")
    else:
        assert result.status == "completed", result
        assert result.exit_code == 0, result
    leftovers = subprocess.run(["docker", "ps", "-aq", "--filter",
        f"label=tomato-agent.request_id={request_id}"], check=True, capture_output=True, timeout=10)
    assert not leftovers.stdout.strip(), "owned container remains after execution"