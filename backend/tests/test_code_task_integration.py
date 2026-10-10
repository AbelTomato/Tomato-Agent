"""HTTP-to-workspace contract checks using a content-aware fake sandbox."""

import ast
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.agent.code_task import CodeTaskStrategy
from app.agent.models import LLMResponse, ToolCall
from app.agent.sandbox import LocalSandboxExecutor
from app.application import create_app
from app.settings import Settings


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requested_tools = []

    async def complete(self, messages, tools):
        response = next(self.responses)
        if response.tool_call is not None:
            self.requested_tools.append(response.tool_call.name)
        return response


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["model", "sandbox"])
async def test_cancel_api_stops_active_execution_and_waits_for_cleanup(tmp_path, phase):
    app = create_app(Settings(
        _env_file=None, database_path=tmp_path / "agent.db", docs_root=tmp_path,
        code_task_workspace_root=tmp_path / "workspaces",
        code_task_artifact_root=tmp_path / "artifacts", llm_api_key="",
        code_task_worker_enabled=True,
        code_task_worker_poll_interval_seconds=0.01,
        knowledge_pipeline_enabled=False, knowledge_rerank_enabled=False,
    ))
    started = asyncio.Event()
    cleaned = asyncio.Event()
    model_calls = 0

    async def block():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.01)
            cleaned.set()

    class LLM:
        async def complete(self, messages, tools):
            nonlocal model_calls
            model_calls += 1
            if phase == "model":
                await block()
            return call("run_tests", {"target": "unit"})

    async def backend(request, profile):
        await block()

    service = app.state.code_task_service
    service.test_tool.executor = LocalSandboxExecutor(backend)
    app.state.code_task_llm = LLM()
    app.state.code_task_strategy = CodeTaskStrategy()
    service.llm = app.state.code_task_llm
    service.strategy = app.state.code_task_strategy
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            created = await client.post("/api/code-tasks", json={"task": "Fix addition"})
            run_id = created.json()["run_id"]
            execution = asyncio.create_task(client.post(f"/api/code-tasks/{run_id}/execute"))
            try:
                await asyncio.wait_for(started.wait(), 2)
                cancelled = await asyncio.wait_for(
                    client.post(f"/api/code-tasks/{run_id}/cancel", json={}), 2,
                )
                assert cancelled.status_code == 200
                assert cancelled.json()["status"] == "cancelled"
                assert cleaned.is_set()
                response = await asyncio.wait_for(execution, 2)
                assert response.status_code == 202
                assert model_calls == 1
                repeated = await client.post(f"/api/code-tasks/{run_id}/cancel", json={})
                assert repeated.json()["status"] == "cancelled"
                persisted = (await client.get(f"/api/code-tasks/{run_id}")).json()
                assert persisted["status"] == "cancelled"
                assert not any(e["event_type"] == "code_task.completed" for e in persisted["events"])
            finally:
                if not execution.done():
                    execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)


def call(name, arguments):
    return LLMResponse(kind="tool_call", tool_call=ToolCall(
        name=name, arguments=arguments,
    ))


def test_configured_provider_is_exposed_to_code_task_routes(tmp_path):
    app = create_app(Settings(
        _env_file=None, database_path=tmp_path / "agent.db", docs_root=tmp_path,
        code_task_workspace_root=tmp_path / "workspaces",
        code_task_artifact_root=tmp_path / "artifacts", llm_api_key="test-only-key",
        knowledge_pipeline_enabled=False, knowledge_rerank_enabled=False,
    ))
    service = app.state.code_task_service
    assert getattr(app.state, "code_task_llm", None) is service.llm
    assert getattr(app.state, "code_task_strategy", None) is service.strategy
    assert getattr(app.state, "code_task_test_tool", None) is service.test_tool


@pytest.mark.asyncio
async def test_failing_fixture_can_be_read_repaired_and_retested_through_api(tmp_path):
    app = create_app(Settings(
        _env_file=None, database_path=tmp_path / "agent.db",
        docs_root=tmp_path, code_task_workspace_root=tmp_path / "workspaces",
        code_task_artifact_root=tmp_path / "artifacts", llm_api_key="",
        code_task_worker_enabled=True,
        code_task_worker_poll_interval_seconds=0.01,
        knowledge_pipeline_enabled=False, knowledge_rerank_enabled=False,
    ))
    llm = ScriptedLLM([
        call("read_file", {"path": "calculator.py"}),
        call("read_file", {"path": "test_calculator.py"}),
        call("run_tests", {"target": "unit"}),
        call("apply_patch", {"changes": [{
            "path": "calculator.py", "old_text": "return left - right",
            "new_text": "return left + right",
        }]}),
        call("run_tests", {"target": "unit"}),
        call("get_diff", {}),
        LLMResponse(kind="final", content="Fixed addition and verified the test."),
    ])
    observed_exit_codes = []

    async def backend(request, profile):
        # Inspect only the fixed fixture AST; never execute generated code or argv.
        assert request.arguments["target"] == "unit"
        assert request.arguments["argv"] == [
            "python", "-m", "pytest", "-q", "test_calculator.py",
        ]
        assert not profile.allow_network and not profile.allow_process
        assert profile.credential_names == ()
        repo = Path(request.arguments["cwd"])
        assert str(repo.resolve()) in profile.allowed_paths
        tree = ast.parse((repo / "calculator.py").read_text(encoding="utf-8"))
        operation = tree.body[0].body[0].value.op
        test_source = (repo / "test_calculator.py").read_text(encoding="utf-8")
        assert "assert add(2, 3) == 5" in test_source
        exit_code = 0 if isinstance(operation, ast.Add) else 1
        observed_exit_codes.append(exit_code)
        return {"exit_code": exit_code,
                "stdout": "1 passed" if exit_code == 0 else "1 failed"}

    service = app.state.code_task_service
    service.test_tool.executor = LocalSandboxExecutor(backend)
    app.state.code_task_llm = llm
    app.state.code_task_strategy = CodeTaskStrategy()
    service.llm = app.state.code_task_llm
    service.strategy = app.state.code_task_strategy
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            created = await client.post("/api/code-tasks", json={"task": "Fix addition"})
            assert created.status_code == 202, created.text
            run_id = created.json()["run_id"]
            executed = await client.post(f"/api/code-tasks/{run_id}/execute")
            assert executed.status_code == 202, executed.text
            async with asyncio.timeout(3):
                while (await client.get(f"/api/code-tasks/{run_id}")).json()["status"] not in {
                    "completed", "failed", "cancelled", "timed_out",
                }:
                    await asyncio.sleep(0.01)
            result = (await client.get(f"/api/code-tasks/{run_id}")).json()
            assert result["status"] == "completed"
            assert observed_exit_codes[0] == 1 and observed_exit_codes[-1] == 0
            assert observed_exit_codes == [1, 0]
            assert result["changed_files"] == ["calculator.py"]
            assert result["test_results"][0]["passed"] is False
            assert result["test_results"][-1]["exit_code"] == 0
            assert result["test_results"][-1]["passed"] is True
            assert result["usage"] and result["tool_calls"] == 6
            assert llm.requested_tools == [
                "read_file", "read_file", "run_tests", "apply_patch", "run_tests", "get_diff",
            ]
            queried = await client.get(f"/api/code-tasks/{run_id}")
            assert queried.status_code == 200
            assert queried.json()["status"] == "completed"
            events = (await client.get(f"/api/code-tasks/{run_id}/events")).json()["events"]
            assert events == result["events"]
            assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
            assert events[0]["event_type"] == "code_task.created"
            assert events[-1]["event_type"] == "code_task.completed"
            for ref in result["artifacts"]:
                artifact = await client.get(
                    f"/api/code-tasks/{run_id}/artifacts/{ref['artifact_id']}"
                )
                assert artifact.status_code == 200
                assert artifact.json()["artifact"] == ref
                if ref["kind"] == "test_report":
                    report = json.loads(artifact.json()["content"])
                    assert report["run_id"] == run_id
                    assert report["passed"] == (report["exit_code"] == 0)
                if ref["kind"] == "diff":
                    diff = artifact.json()["content"]
                    assert diff.startswith("diff --git a/calculator.py b/calculator.py")
                    assert "+    return left + right" in diff
                    assert "diff --git a/test_calculator.py" not in diff


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario, expected_status, expected_code", [
    ("escape", "failed", "permission_denied"),
    ("unknown_target", "failed", "invalid_arguments"),
    ("timeout", "timed_out", "timeout"),
    ("unavailable", "failed", "backend_unavailable"),
    ("nonzero", "failed", "tests_failed"),
    ("cancel", "cancelled", "cancelled"),
])
async def test_unsafe_or_unsuccessful_execution_never_completes(
    tmp_path, scenario, expected_status, expected_code,
):
    app = create_app(Settings(
        _env_file=None, database_path=tmp_path / "agent.db", docs_root=tmp_path,
        code_task_workspace_root=tmp_path / "workspaces",
        code_task_artifact_root=tmp_path / "artifacts", llm_api_key="",
        code_task_worker_enabled=True,
        code_task_worker_poll_interval_seconds=0.01,
        knowledge_pipeline_enabled=False, knowledge_rerank_enabled=False,
    ))
    first = call("run_tests", {"target": "unit"})
    if scenario == "escape":
        first = call("write_file", {"path": "../outside.py", "content": "bad"})
    elif scenario == "unknown_target":
        first = call("run_tests", {"target": "unknown"})
    llm = ScriptedLLM([first, LLMResponse(kind="final", content="Everything passed")])
    app.state.code_task_llm = llm
    app.state.code_task_strategy = CodeTaskStrategy()
    calls = []

    async def backend(request, profile):
        calls.append(request)
        if scenario == "timeout":
            raise TimeoutError
        if scenario == "cancel":
            raise asyncio.CancelledError
        return {"exit_code": 1, "stdout": "1 failed"}

    if scenario != "unavailable":
        app.state.code_task_service.test_tool.executor = LocalSandboxExecutor(backend)
    app.state.code_task_service.llm = llm
    app.state.code_task_service.strategy = app.state.code_task_strategy
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            created = await client.post("/api/code-tasks", json={"task": "Fix addition"})
            assert created.status_code == 202
            run_id = created.json()["run_id"]
            response = await client.post(f"/api/code-tasks/{run_id}/execute")
            assert response.status_code == 202, response.text
            async with asyncio.timeout(3):
                while (await client.get(f"/api/code-tasks/{run_id}")).json()["status"] not in {
                    "completed", "failed", "cancelled", "timed_out",
                }:
                    await asyncio.sleep(0.01)
            persisted = (await client.get(f"/api/code-tasks/{run_id}")).json()
            assert persisted["status"] == expected_status
            assert persisted["error"]["code"] == expected_code
            assert persisted["events"][-1]["event_type"] == "code_task.failed"
            assert not any(event["event_type"] == "code_task.completed"
                           for event in persisted["events"])
            if scenario in {"escape", "unknown_target", "unavailable"}:
                assert calls == []
            else:
                assert len(calls) == 1
            assert not list((tmp_path / "workspaces").glob("*/outside.py"))