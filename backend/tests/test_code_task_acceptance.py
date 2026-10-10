"""Explicit opt-in real provider and sandbox HTTP acceptance."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.application import create_app
from app.settings import Settings


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_CODE_TASK_ACCEPTANCE") != "1",
    reason="explicit real provider and sandbox opt-in required",
)


@pytest.mark.asyncio
async def test_real_provider_repairs_fixture_through_http():
    root = Path(__file__).resolve().parents[1] / "data" / "code-task-acceptance" / uuid4().hex
    root.mkdir(parents=True)
    config = Settings(
        database_path=root / "runs.db", docs_root=root,
        code_task_workspace_root=root / "workspaces",
        code_task_artifact_root=root / "artifacts",
        knowledge_pipeline_enabled=False, knowledge_rerank_enabled=False,
        code_task_tool_timeout_seconds=60, code_task_max_duration_seconds=300,
        code_task_max_loops=20, code_task_max_tool_calls=16,
        code_task_worker_enabled=True,
    )
    assert config.llm_api_key.strip(), "real provider configuration required"
    assert config.code_task_sandbox_backend in {"docker", "openshell"}
    app = create_app(config)
    summary = {"model": config.llm_model, "sandbox": config.code_task_sandbox_backend,
               "tool_timeout_seconds": 60, "duration_seconds": 300}
    provider = app.state.code_task_llm
    calls = []
    summary["provider_calls"] = calls

    class ObservedProvider:
        async def complete(self, messages, tools):
            observation = {"index": len(calls) + 1}
            calls.append(observation)
            try:
                response = await provider.complete(messages, tools)
                observation.update({"kind": response.kind,
                                    "tool": response.tool_call.name if response.tool_call else None})
                return response
            except Exception as exc:
                observation["error_type"] = type(exc).__name__
                raise

    app.state.code_task_llm = ObservedProvider()
    app.state.code_task_service.llm = app.state.code_task_llm
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=330,
            ) as client:
                created = await client.post("/api/code-tasks", json={"task": (
                    "Fix the addition bug in calculator.py. Read calculator.py and "
                    "test_calculator.py, run the unit target first to observe failure, "
                    "then repair only calculator.py using apply_patch, run the unit "
                    "target again and call get_diff before your final response. "
                    "Do not modify the test or add files."
                )})
                assert created.status_code == 202
                run_id = created.json()["run_id"]
                response = await client.post(f"/api/code-tasks/{run_id}/execute")
                assert response.status_code == 202
                async with asyncio.timeout(330):
                    while True:
                        persisted = await client.get(f"/api/code-tasks/{run_id}")
                        assert persisted.status_code == 200
                        payload = persisted.json()
                        if payload["status"] in {"completed", "failed", "cancelled", "timed_out", "waiting"}:
                            break
                        await asyncio.sleep(0.25)
                summary.update({"run_id": run_id, "http_status": response.status_code,
                                "status": payload["status"], "error": payload.get("error"),
                                "usage": payload["usage"], "tool_calls": payload["tool_calls"],
                                "changed_files": payload["changed_files"],
                                "test_results": payload["test_results"]})
                assert response.status_code == 202, summary
                assert payload["status"] == "completed", summary
                assert payload["changed_files"] == ["calculator.py"]
                assert [r["exit_code"] for r in payload["test_results"]] == [1, 0]
                assert payload["usage"]["model_calls"] > 0
                assert payload["diff_artifact"]
                for ref in payload["artifacts"]:
                    artifact = await client.get(f"/api/code-tasks/{run_id}/artifacts/{ref['artifact_id']}")
                    assert artifact.status_code == 200
                    if ref["kind"] == "diff":
                        assert "diff --git a/test_calculator.py" not in artifact.json()["content"]
                sequences = [event["sequence"] for event in payload["events"]]
                assert sequences == list(range(1, len(sequences) + 1))
                summary["accepted"] = True
    finally:
        (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"Acceptance report: {root / 'summary.json'}")