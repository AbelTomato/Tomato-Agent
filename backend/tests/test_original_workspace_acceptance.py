"""Opt-in real Provider + digest-pinned Docker, with isolated retained artifacts."""

import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.application import create_app
from app.settings import Settings
from test_unified_mvp_integration import advance, submit


pytestmark = pytest.mark.skipif(os.environ.get("RUN_ORIGINAL_WORKSPACE_ACCEPTANCE") != "1",
    reason="explicit original workspace acceptance opt-in required")


async def test_real_provider_docker_two_rounds():
    root = Path(__file__).parents[1] / "data" / "unified-mvp-acceptance" / str(uuid4())
    project = root / "project"
    project.mkdir(parents=True)
    project.chmod(0o755)
    (project / "calculator.py").write_text("def add(a, b):\n    return a - b\n\ndef multiply(a, b):\n    return a + b\n")
    (project / "test_calculator.py").write_text("from calculator import add, multiply\ndef test_add():\n    assert add(2, 3) == 5\ndef test_multiply():\n    assert multiply(2, 2) == 4\n")
    (project / "notes.txt").write_text("pre-existing user modification\n")
    config = Settings(database_path=root / "agent.db", docs_root=project,
        draft_directory=root / "drafts", code_task_workspace_root=root / "metadata",
        code_task_artifact_root=root / "artifacts", workspace_allowed_roots=[project],
        task_worker_enabled=True, code_task_sandbox_backend="docker",
        code_task_docker_image=os.environ["SANDBOX_IMAGE"],
        code_task_max_duration_seconds=300, code_task_tool_timeout_seconds=60,
        code_task_max_loops=20, code_task_max_tool_calls=16)
    assert config.llm_api_key.strip(), "real Provider must be configured"
    app = create_app(config)
    deps = app.state.dependencies
    # Initialize isolated repositories without starting a racing background poller.
    for repository in (deps.session_repository, deps.run_repository,
                       deps.writing_repository, deps.writing_execution_repository):
        await repository.init()
    await deps.code_task_service.initialize()
    summary = {"project": str(project), "image": config.code_task_docker_image, "runs": []}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            session = (await client.post("/api/sessions", json={})).json()["session_id"]
            registered = await client.post("/api/workspaces", json={"path": str(project), "pytest_paths": ["test_calculator.py"]})
            assert registered.status_code == 201, registered.text
            workspace = registered.json()["workspace_id"]
            for function, operator, previous in (("add", "+", "-"), ("multiply", "*", "+")):
                run = await submit(client, session, "code",
                    f"Read calculator.py and test_calculator.py. Fix only {function} to return a {operator} b; preserve the other function and all other files. Do not edit tests. Run unit after the edit and call get_diff, then finish.",
                    workspace_id=workspace)
                run = await advance(client, deps, run)
                summary["runs"].append(run)
                assert run["status"] == "completed", run
                assert run["result"]["test_results"][-1]["passed"] is True
                diff = next(item for item in run["result"]["artifacts"] if item["kind"] == "diff")
                content = (await client.get(f"/api/code-tasks/{run['run_id']}/artifacts/{diff['artifact_id']}")).json()["content"]
                assert f"-    return a {previous} b" in content and f"+    return a {operator} b" in content
                assert "notes.txt" not in content and "test_calculator.py" not in content
                assert (project / "notes.txt").read_text() == "pre-existing user modification\n"
            assert summary["runs"][-1]["result"]["test_results"][-1]["passed"] is True
    finally:
        (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))