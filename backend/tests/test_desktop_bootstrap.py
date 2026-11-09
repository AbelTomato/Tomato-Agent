import asyncio
import json
import os
import subprocess
import sys
import socket
from pathlib import Path

import httpx
import pytest

from app.desktop.bootstrap import configuration


def launch(root, *, token="a" * 64, port=0):
    result = subprocess.run(
        [sys.executable, "-m", "app.desktop.bootstrap"],
        input=json.dumps({"token": token, "owner": "owner", "environment_id": "Ubuntu", "acceptance_root": str(root), "port": port}) + "\n",
        text=True, capture_output=True, timeout=25,
        env={**os.environ, "PYTHONPATH": str(Path.cwd())},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(os.name == "nt", reason="The WSL singleton launcher uses POSIX file locks")
def test_bootstrap_singleton_identity_reconnect_and_owned_shutdown(tmp_path):
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        requested_port = candidate.getsockname()[1]
    first = launch(tmp_path, port=requested_port)
    assert first["port"] == requested_port
    headers = {"Authorization": "Bearer " + "a" * 64,
               "X-Tomato-Instance": first["service_instance_id"], "X-Tomato-Client": "first",
               "X-Tomato-Owner": "owner"}
    with httpx.Client(base_url=f"http://127.0.0.1:{first['port']}", headers=headers, trust_env=False) as client:
        try:
            assert first["owned"] is True
            assert (tmp_path / "agent.db").exists()
            second = launch(tmp_path)
            assert second["owned"] is False
            assert second["port"] == first["port"]
            assert second["service_instance_id"] == first["service_instance_id"]
            assert client.get("/health", headers={"Authorization": "wrong"}).status_code == 401
            assert client.post("/api/desktop/attach").status_code == 200
            session = client.post("/api/sessions", json={}).json()["session_id"]
            assert client.get(f"/api/sessions/{session}/messages").status_code == 200
            assert client.post("/api/desktop/close").json()["shutdown"] is True
        finally:
            # Only the service started above is eligible for this authenticated close.
            try:
                client.post("/api/desktop/attach")
                client.post("/api/desktop/close")
            except httpx.HTTPError:
                pass


def test_bootstrap_rejects_bad_input_without_echoing_secret(tmp_path):
    result = subprocess.run([sys.executable, "-m", "app.desktop.bootstrap"],
                            input='{"token":"private-invalid"}\n', text=True,
                            capture_output=True, timeout=10,
                            env={**os.environ, "TOMATO_AGENT_ENGINE_DEBUG": "1"})
    assert result.returncode != 0
    assert "private-invalid" not in result.stderr + result.stdout
    assert "ValueError" in result.stderr


def test_managed_engine_configuration_uses_explicit_data_and_resource_directories(tmp_path):
    data = tmp_path / "application-data"
    resources = tmp_path / "resources"
    drafts = tmp_path / "local-application-data" / "drafts"
    value = {"environment_id": "windows-local", "data_dir": str(data),
             "resource_dir": str(resources), "draft_dir": str(drafts),
             "llm_api_key": "ephemeral-secret",
             "llm_base_url": "https://provider.invalid/v1", "llm_model": "model"}
    config = configuration(value)
    assert config.database_path == data / "agent.db"
    assert config.docs_root == resources / "docs"
    assert config.draft_directory == drafts
    assert config.llm_api_key == "ephemeral-secret"
    assert config.effective_task_worker_enabled


def test_managed_engine_rejects_relative_draft_directory(tmp_path):
    from app.desktop.bootstrap import read_input

    input_data = {"token": "a" * 64, "owner": "owner", "data_dir": str(tmp_path / "data"),
                  "resource_dir": str(tmp_path / "resources"), "draft_dir": "drafts"}
    original_stdin = sys.stdin
    try:
        from io import StringIO
        sys.stdin = StringIO(json.dumps(input_data) + "\n")
        with pytest.raises(ValueError, match="absolute draft directory"):
            read_input()
    finally:
        sys.stdin = original_stdin


@pytest.mark.asyncio
async def test_managed_engine_process_authenticates_and_exits_through_owner_close(tmp_path):
    token = "a" * 64
    owner = "owner-1"
    data_dir = tmp_path / "data"
    resources = tmp_path / "resources"
    resources.mkdir()
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.desktop.bootstrap", "--serve",
        cwd=str(Path(__file__).resolve().parents[1]),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    process.stdin.write((json.dumps({"token": token, "owner": owner, "environment_id": "test",
        "data_dir": str(data_dir), "resource_dir": str(resources)}) + "\n").encode())
    await process.stdin.drain()
    process.stdin.close()
    startup = json.loads(await asyncio.wait_for(process.stdout.readline(), timeout=15))
    endpoint = f"http://127.0.0.1:{startup['port']}"
    headers = {"Authorization": f"Bearer {token}", "X-Tomato-Instance": startup["service_instance_id"]}
    async with httpx.AsyncClient() as client:
        unauthorized = await client.get(f"{endpoint}/api/desktop/connection")
        assert unauthorized.status_code == 401
        attached = await client.post(f"{endpoint}/api/desktop/attach",
            headers={**headers, "X-Tomato-Client": "desktop-1"})
        assert attached.status_code == 200
        closed = await client.post(f"{endpoint}/api/desktop/close",
            headers={**headers, "X-Tomato-Client": "desktop-1", "X-Tomato-Owner": owner})
        assert closed.json()["shutdown"]
    await asyncio.wait_for(process.wait(), timeout=5)
    assert process.returncode == 0
    assert (data_dir / "agent.db").exists()
