"""Independent API/Worker process for opt-in real sandbox restart evidence."""

import asyncio
import json
from pathlib import Path
import sys

import httpx

from app.application import create_app
from app.agent.code_task import CodeTaskStrategy
from app.agent.models import LLMResponse
from app.settings import Settings
from test_code_task_integration import ScriptedLLM, call


async def main():
    root, mode, scenario = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    config = Settings(
        database_path=root / "runs.db", docs_root=root,
        code_task_workspace_root=root / "workspaces",
        code_task_artifact_root=root / "artifacts",
        code_task_worker_enabled=mode == "resume" or scenario == "sandbox_unknown",
        code_task_worker_poll_interval_seconds=0.05,
        code_task_tool_timeout_seconds=60, code_task_max_duration_seconds=180,
        knowledge_pipeline_enabled=False, knowledge_rerank_enabled=False,
    )
    assert config.code_task_sandbox_backend == "openshell"
    app = create_app(config)
    service = app.state.code_task_service
    llm = ScriptedLLM([
        call("apply_patch", {"changes": [{"path": "calculator.py",
             "old_text": "return left - right", "new_text": "return left + right"}]}),
        call("run_tests", {"target": "unit"}), call("get_diff", {}),
        LLMResponse(kind="final", content="Fixed and verified."),
    ])
    service.llm = app.state.code_task_llm = llm
    service.strategy = app.state.code_task_strategy = CodeTaskStrategy()
    if mode == "create" and scenario == "sandbox_unknown":
        executor = service.test_tool.executor
        original = executor._run
        name = None

        async def barrier(command, *args):
            nonlocal name
            if command[2] == "create":
                name = command[4]
            if command[2] == "exec":
                print(json.dumps({"barrier": "sandbox_uploaded", "name": name}), flush=True)
                await asyncio.Event().wait()
            return await original(command, *args)

        executor._run = barrier
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            if mode == "create":
                created = await client.post("/api/code-tasks", json={"task": "Restart acceptance"})
                assert created.status_code == 202
                run_id = created.json()["run_id"]
                (root / "run-id").write_text(run_id)
                if scenario == "queued":
                    print(json.dumps({"barrier": "queued", "run_id": run_id}), flush=True)
                await asyncio.Event().wait()
            else:
                run_id = (root / "run-id").read_text()
                async with asyncio.timeout(90):
                    while True:
                        payload = (await client.get(f"/api/code-tasks/{run_id}")).json()
                        if payload["status"] in {"completed", "failed", "waiting", "cancelled", "timed_out"}:
                            break
                        await asyncio.sleep(0.1)
                print(json.dumps({"status": payload["status"], "error": payload.get("error"),
                                  "artifacts": payload["artifacts"]}), flush=True)


asyncio.run(main())