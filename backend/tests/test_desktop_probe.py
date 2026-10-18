import json
import threading
from http.client import HTTPConnection
from http.server import HTTPServer

import pytest

from app.desktop.probe import make_handler


@pytest.fixture
def probe_server(tmp_path):
    (tmp_path / "含 空格目录").mkdir()
    (tmp_path / "outside-link").symlink_to(tmp_path, target_is_directory=True)
    server = HTTPServer(("127.0.0.1", 0), make_handler("a" * 64, "instance", tmp_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, tmp_path
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.parametrize("token,instance,status", [
    (None, "instance", 401), ("wrong", "instance", 401), ("a" * 64, "wrong", 409),
])
def test_probe_rejects_missing_wrong_credentials_and_instance(probe_server, token, instance, status):
    server, _ = probe_server
    client = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    headers = {"X-Tomato-Instance": instance}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    client.request("GET", "/health", headers=headers)
    response = client.getresponse()
    assert response.status == status
    assert "a" * 64 not in response.read().decode()
    client.close()


def test_probe_identity_directory_and_log_redaction(probe_server, capsys):
    server, root = probe_server
    for route in ("health", "identity", "directory"):
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        client.request("GET", f"/{route}", headers={
            "Authorization": "Bearer " + "a" * 64, "X-Tomato-Instance": "instance",
        })
        response = client.getresponse()
        assert response.status == 200
        data = json.loads(response.read())
        if route == "directory":
            assert data == {"root": str(root), "directories": ["含 空格目录"]}
        else:
            assert data["protocol"] == 1
            assert data["service_instance_id"] == "instance"
        client.close()
    captured = capsys.readouterr()
    assert "a" * 64 not in captured.out + captured.err
    assert not (root / "agent.db").exists()