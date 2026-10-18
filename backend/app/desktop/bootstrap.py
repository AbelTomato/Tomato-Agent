"""Fixed WSL entrypoint. No shell, credential argv, or business DB in acceptance mode."""

import asyncio
import fcntl
import json
import os
from pathlib import Path
import secrets
import select
import socket
import subprocess
import sys

import httpx
import uvicorn

from app.application import create_app
from app.desktop.connection import install_desktop
from app.settings import Settings


def read_input():
    value = json.loads(sys.stdin.readline(16384))
    token = value.get("token", "")
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("invalid input")
    if not isinstance(value.get("owner"), str) or not value["owner"]:
        raise ValueError("invalid owner")
    return value


def configuration(value):
    if value.get("acceptance_root"):
        root = Path(value["acceptance_root"])
        if not root.is_absolute():
            raise ValueError("absolute acceptance root required")
        return Settings(_env_file=None, config_file=None, database_path=root / "agent.db",
                        docs_root=root, draft_directory=root / "drafts",
                        code_task_workspace_root=root / "workspaces",
                        code_task_artifact_root=root / "artifacts", llm_api_key="",
                        embedding_api_key="", task_worker_enabled=False)
    return Settings()


async def serve(value):
    config = configuration(value)
    app = create_app(config)
    identity_path = config.database_path.parent / ".desktop-instance"
    if not identity_path.exists():
        identity_path.write_text(secrets.token_hex(16))
        identity_path.chmod(0o600)
    instance = identity_path.read_text().strip()
    sock = socket.socket()
    port_request = value.get("port", 0)
    if type(port_request) is not int or not 0 <= port_request <= 65535:
        raise ValueError("invalid port")
    sock.bind(("127.0.0.1", port_request))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    install_desktop(app, token=value["token"], instance=instance, owner=value["owner"],
                    stop=lambda: setattr(server, "should_exit", True))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            await task
            raise RuntimeError("startup failed")
        await asyncio.sleep(0.02)
    print(json.dumps({"protocol": 1, "port": port, "service_instance_id": instance}), flush=True)
    # The launcher must finish without waiting for this independent server.
    sys.stdout.close()
    await task


def bootstrap(value):
    config = configuration(value)
    root = config.database_path.parent
    root.mkdir(parents=True, exist_ok=True)
    # Lock excludes concurrent launchers and is released even on failure.
    with (root / ".desktop-launch.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = root / ".desktop-service.json"
        if manifest.exists():
            previous = json.loads(manifest.read_text())
            with httpx.Client(trust_env=False, timeout=2) as client:
                try:
                    response = client.get(f"http://127.0.0.1:{int(previous['port'])}/api/desktop/connection",
                                          headers={"Authorization": f"Bearer {value['token']}",
                                                   "X-Tomato-Instance": previous["service_instance_id"]})
                except httpx.ConnectError:
                    pass  # Only definite connection refusal permits a new process.
                else:
                    response.raise_for_status()
                    identity = response.json()
                    if identity["protocol"] != 1 or identity["service_instance_id"] != previous["service_instance_id"]:
                        raise ValueError("instance mismatch")
                    print(json.dumps({**previous, "owned": False}), flush=True)
                    return
        child = subprocess.Popen([sys.executable, "-m", "app.desktop.bootstrap", "--serve"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, start_new_session=True, text=True)
        child.stdin.write(json.dumps(value) + "\n")
        child.stdin.close()
        if not select.select([child.stdout], [], [], 18)[0]:
            # Ownership is certain; terminate only this not-yet-ready child.
            child.terminate()
            raise TimeoutError("startup timeout")
        result = json.loads(child.stdout.readline())
        child.stdout.close()
        manifest.write_text(json.dumps(result))
        manifest.chmod(0o600)
        print(json.dumps({**result, "owned": True}), flush=True)


def main():
    try:
        value = read_input()
        if sys.argv[1:] == ["--serve"]:
            asyncio.run(serve(value))
        elif not sys.argv[1:]:
            bootstrap(value)
        else:
            raise ValueError("invalid arguments")
    except Exception:
        # No exception repr/traceback: configuration and input can carry secrets.
        print("Desktop service startup failed; check installation and connection identity.", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()