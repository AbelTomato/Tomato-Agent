"""HTTP-to-workspace contract checks using a content-aware fake sandbox."""

import ast
import asyncio
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

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


def test_tool_policy_digest_is_stable_across_process_hash_seeds():
    import subprocess

    source = """from app.agent.harness_models import ToolPolicy
from app.execution.ports import canonical_digest
policy = ToolPolicy(allowed_tools=frozenset({'list_files', 'read_file', 'search_files',
    'write_file', 'apply_patch', 'get_diff', 'collect_artifact', 'run_tests'}),
    max_calls=8, timeout_seconds=60, max_result_chars=1000)
print(canonical_digest(policy.model_dump(mode='json')))
"""
    digests = [subprocess.check_output(
        [sys.executable, "-c", source], cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONHASHSEED": seed}, text=True, timeout=10,
    ).strip() for seed in ("1", "3", "42", "99")]
    assert len(set(digests)) == 1, digests


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


@pytest.mark.skipif(
    os.environ.get("RUN_BACKGROUND_SANDBOX_ACCEPTANCE") != "1",
    reason="explicit real background sandbox acceptance opt-in required",
)
@pytest.mark.parametrize("scenario", ["success", "tests_failed", "cancelled", "cleanup_failed"])
async def test_real_background_sandbox_boundaries(scenario, monkeypatch):
    """Real sandbox and HTTP; scripted model and injected cleanup fault are explicit."""
    from app.agent.sandbox_openshell import OpenShellSandboxExecutor

    root = Path(__file__).resolve().parents[1] / "data" / "background-sandbox-acceptance" / uuid4().hex
    root.mkdir(parents=True)
    config = Settings(
        database_path=root / "runs.db", docs_root=root,
        code_task_workspace_root=root / "workspaces",
        code_task_artifact_root=root / "artifacts",
        code_task_worker_enabled=True, code_task_worker_poll_interval_seconds=0.05,
        code_task_tool_timeout_seconds=60, code_task_max_duration_seconds=180,
        knowledge_pipeline_enabled=False, knowledge_rerank_enabled=False,
    )
    assert config.code_task_sandbox_backend == "openshell"
    app = create_app(config)
    service = app.state.code_task_service
    executor = OpenShellSandboxExecutor(
        config.code_task_openshell_image, openshell_binary=config.code_task_openshell_binary,
    )
    service.test_tool.executor = executor
    responses = []
    if scenario in {"success", "cleanup_failed"}:
        responses.append(call("apply_patch", {"changes": [{
            "path": "calculator.py", "old_text": "return left - right",
            "new_text": "return left + right",
        }]}))
    responses.append(call("run_tests", {"target": "unit"}))
    if scenario == "success":
        responses.append(call("get_diff", {}))
    responses.append(LLMResponse(kind="final", content="Tests finished."))
    llm = ScriptedLLM(responses)
    service.llm = app.state.code_task_llm = llm
    service.strategy = app.state.code_task_strategy = CodeTaskStrategy()
    executing = asyncio.Event()
    names = []
    original_run = executor._run
    original_cleanup = executor._cleanup
    summary = {"scenario": scenario, "model": "scripted", "sandbox": "real_openshell",
               "cleanup_fault_injected": scenario == "cleanup_failed"}

    async def observe(command, *args):
        if command[2] == "create":
            names.append(command[4])
        if command[2] == "exec":
            executing.set()
        return await original_run(command, *args)

    async def refuse_cleanup(name, timeout):
        return False

    monkeypatch.setattr(executor, "_run", observe)
    if scenario == "cleanup_failed":
        monkeypatch.setattr(executor, "_cleanup", refuse_cleanup)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            ) as client:
                created = await client.post("/api/code-tasks", json={"task": "Check fixed fixture"})
                assert created.status_code == 202
                run_id = created.json()["run_id"]
                summary["run_id"] = run_id
                if scenario == "cancelled":
                    await asyncio.wait_for(executing.wait(), 65)
                    cancelled = await client.post(f"/api/code-tasks/{run_id}/cancel", json={})
                    assert cancelled.status_code == 200
                async with asyncio.timeout(180):
                    while True:
                        payload = (await client.get(f"/api/code-tasks/{run_id}")).json()
                        if payload["status"] in {"completed", "failed", "cancelled", "timed_out", "waiting"}:
                            break
                        await asyncio.sleep(0.1)
                summary.update(status=payload["status"], error=payload.get("error"),
                               artifacts=payload["artifacts"], test_results=payload["test_results"])
                expected = {"success": ("completed", None), "tests_failed": ("failed", "tests_failed"),
                            "cancelled": ("cancelled", None),
                            "cleanup_failed": ("failed", "cleanup_failed")}[scenario]
                assert payload["status"] == expected[0], summary
                assert (payload.get("error") or {}).get("code") == expected[1], summary
                if scenario == "cancelled":
                    assert any(event["event_type"] == "execution.cancelled" for event in payload["events"])
                for ref in payload["artifacts"]:
                    artifact = await client.get(f"/api/code-tasks/{run_id}/artifacts/{ref['artifact_id']}")
                    assert artifact.status_code == 200
                if scenario != "cancelled":
                    assert any(ref["kind"] == "test_report" for ref in payload["artifacts"]), summary
                assert names
                summary["owned_before_final_cleanup"] = [await executor._owned(name) for name in names]
                assert summary["owned_before_final_cleanup"] == [scenario == "cleanup_failed"]
                summary["accepted"] = True
    finally:
        # Only sandboxes created by this test are eligible for cleanup.
        summary["final_cleanup"] = [await original_cleanup(name, 10) for name in names]
        summary["owned_after_final_cleanup"] = [await executor._owned(name) for name in names]
        (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"Acceptance report: {root / 'summary.json'}")
        assert all(summary["final_cleanup"]), summary
        assert not any(summary["owned_after_final_cleanup"]), summary


@pytest.mark.skipif(
    os.environ.get("RUN_BACKGROUND_SANDBOX_ACCEPTANCE") != "1",
    reason="explicit real background sandbox acceptance opt-in required",
)
@pytest.mark.parametrize("scenario", ["queued", "sandbox_unknown"])
async def test_real_sandbox_api_worker_process_restart(scenario):
    from app.agent.sandbox_openshell import OpenShellSandboxExecutor

    backend = Path(__file__).resolve().parents[1]
    root = backend / "data" / "background-sandbox-acceptance" / uuid4().hex
    root.mkdir(parents=True)
    script = backend / "tests" / "fixtures" / "background_sandbox_worker.py"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(backend), str(backend / "tests")])}
    config = Settings()
    executor = OpenShellSandboxExecutor(config.code_task_openshell_image,
                                       openshell_binary=config.code_task_openshell_binary)
    name = None
    summary = {"scenario": scenario, "model": "scripted", "sandbox": "real_openshell"}
    processes = []
    try:
        child = await asyncio.create_subprocess_exec(
            sys.executable, str(script), str(root), "create", scenario,
            cwd=backend, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        processes.append(child)
        line = await asyncio.wait_for(child.stdout.readline(), 80)
        assert line, (await child.stderr.read()).decode()
        barrier = json.loads(line)
        name = barrier.get("name")
        summary["barrier"] = barrier
        if name:
            assert await executor._owned(name)
        child.kill()
        assert await asyncio.wait_for(child.wait(), 10) == -9
        if scenario == "sandbox_unknown":
            # Real lease expiry, without editing persistence or fabricating observations.
            await asyncio.sleep(31)
        restarted = await asyncio.create_subprocess_exec(
            sys.executable, str(script), str(root), "resume", scenario,
            cwd=backend, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        processes.append(restarted)
        out, err = await asyncio.wait_for(restarted.communicate(), 100)
        assert restarted.returncode == 0, err.decode()
        result = json.loads(out)
        summary.update(result)
        assert result["status"] == ("completed" if scenario == "queued" else "waiting"), summary
        if name:
            summary["owned_after_restart"] = await executor._owned(name)
            assert summary["owned_after_restart"] is True
        else:
            assert any(ref["kind"] == "test_report" for ref in result["artifacts"])
            assert any(ref["kind"] == "diff" for ref in result["artifacts"])
        summary["accepted"] = True
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
            await asyncio.wait_for(process.wait(), 10)
        if name:
            summary["manual_cleanup"] = await executor._cleanup(name, 10)
            summary["owned_after_manual_cleanup"] = await executor._owned(name)
        (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"Acceptance report: {root / 'summary.json'}")
        if name:
            assert summary["manual_cleanup"] is True
            assert summary["owned_after_manual_cleanup"] is False