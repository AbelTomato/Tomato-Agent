import json
import os
import subprocess
import sys
import socket
from pathlib import Path

import httpx


def launch(root, *, token="a" * 64, port=0):
    result = subprocess.run(
        [sys.executable, "-m", "app.desktop.bootstrap"],
        input=json.dumps({"token": token, "owner": "owner", "environment_id": "Ubuntu", "acceptance_root": str(root), "port": port}) + "\n",
        text=True, capture_output=True, timeout=25,
        env={**os.environ, "PYTHONPATH": str(Path.cwd())},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


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
                            capture_output=True, timeout=10)
    assert result.returncode != 0
    assert "private-invalid" not in result.stderr + result.stdout