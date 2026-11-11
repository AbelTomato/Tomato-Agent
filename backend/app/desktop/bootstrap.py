"""Fixed WSL entrypoint. No shell, credential argv, or business DB in acceptance mode."""

import asyncio
import json
import os
from pathlib import Path
import secrets
import select
import socket
import subprocess
import sys
import traceback
try:
    import fcntl
except ImportError:
    fcntl = None

import httpx
import uvicorn

from app.application import create_app
from app.desktop.connection import install_desktop
from app.settings import Settings


def read_input():
    value = json.loads(sys.stdin.readline(65536))
    token = value.get("token", "")
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("invalid input")
    if not isinstance(value.get("owner"), str) or not value["owner"]:
        raise ValueError("invalid owner")
    if value.get("data_dir") is not None:
        data_dir = Path(value["data_dir"])
        if not data_dir.is_absolute():
            raise ValueError("absolute data directory required")
        value["data_dir"] = str(data_dir)
        resource_dir = Path(value.get("resource_dir", ""))
        if not resource_dir.is_absolute():
            raise ValueError("absolute resource directory required")
        value["resource_dir"] = str(resource_dir)
        if value.get("draft_dir") is not None:
            draft_dir = Path(value["draft_dir"])
            if not draft_dir.is_absolute():
                raise ValueError("absolute draft directory required")
            value["draft_dir"] = str(draft_dir)
    environment_id = value.get("environment_id", "local")
    if not isinstance(environment_id, str) or not environment_id.strip() or len(environment_id) > 128:
        raise ValueError("invalid environment")
    value["environment_id"] = environment_id
    memory_enabled = value.get("memory_enabled", False)
    memory_auto_mode = value.get("memory_auto_mode", "off")
    if type(memory_enabled) is not bool or memory_auto_mode not in {"off", "automatic", "review"}:
        raise ValueError("invalid memory settings")
    if not memory_enabled and memory_auto_mode != "off":
        raise ValueError("memory mode requires memory to be enabled")
    value["memory_enabled"] = memory_enabled
    value["memory_auto_mode"] = memory_auto_mode
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
                        embedding_api_key="", task_worker_enabled=False,
                        desktop_environment_id=value["environment_id"],
                        desktop_service_instance_id="pending")
    if value.get("data_dir"):
        root = Path(value["data_dir"])
        resource = Path(value["resource_dir"])
        root.mkdir(parents=True, exist_ok=True)
        return Settings(
            _env_file=None, config_file=None, database_path=root / "agent.db",
            docs_root=resource / "docs",
            draft_directory=Path(value.get("draft_dir", root / "drafts")),
            code_task_workspace_root=root / "workspaces",
            code_task_artifact_root=root / "artifacts",
            llm_api_key=value.get("llm_api_key", ""),
            llm_base_url=value.get("llm_base_url", "https://api.openai.com/v1"),
            llm_model=value.get("llm_model", ""),
            embedding_api_key=value.get("embedding_api_key", ""),
            embedding_base_url=value.get("embedding_base_url", "https://api.openai.com/v1"),
            embedding_model=value.get("embedding_model", ""),
            embedding_dimensions=value.get("embedding_dimensions", 0),
            assistant_enabled=True, task_worker_enabled=True,
            memory_enabled=value["memory_enabled"],
            memory_auto_mode=value["memory_auto_mode"],
            desktop_environment_id=value["environment_id"],
            desktop_service_instance_id="pending")
    return Settings(
        desktop_environment_id=value["environment_id"],
        memory_enabled=value["memory_enabled"],
        memory_auto_mode=value["memory_auto_mode"],
    )


async def serve(value):
    config = configuration(value)
    app = create_app(config)
    identity_path = config.database_path.parent / ".desktop-instance"
    if not identity_path.exists():
        identity_path.write_text(secrets.token_hex(16))
        identity_path.chmod(0o600)
    instance = identity_path.read_text().strip()
    app.state.project_service.service_instance_id = instance
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
    if fcntl is None:
        raise RuntimeError("Windows engine must be launched under the desktop process owner")
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
    except Exception as error:
        # No exception repr/traceback: configuration and input can carry secrets.
        print("Desktop service startup failed; check installation and connection identity.", file=sys.stderr)
        if os.environ.get("TOMATO_AGENT_ENGINE_DEBUG") == "1":
            print(type(error).__name__, file=sys.stderr)
            traceback.print_tb(error.__traceback__, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
