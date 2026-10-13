import pytest

from app.tools.base import ToolContext
from test_application_lifecycle import make_app
from app.agent.policies import CodeTaskCapabilityProfile
from app.agent.sandbox import SandboxResult


async def test_tests_catalog_is_unavailable_without_docker(tmp_path):
    app = make_app(tmp_path, assistant_enabled=True)
    assistant = app.state.task_service.assistant
    descriptor = assistant.registry.resolve("run_tests")
    assert descriptor.side_effect == descriptor.recovery_class == "sandbox"
    assert not descriptor.available
    assert descriptor.reason == "docker_not_configured"
    assert "run_tests" not in {item.name for item in assistant.registry.definitions()}


async def test_docker_catalog_exposes_only_registered_target(tmp_path):
    app = make_app(tmp_path, assistant_enabled=True, code_task_sandbox_backend="docker",
                   code_task_docker_image="pytest@sha256:" + "a" * 64)
    assistant = app.state.task_service.assistant
    descriptor = assistant.registry.resolve("run_tests")
    assert descriptor.available
    assert descriptor.constraints["executor"] == "docker"
    tool = assistant.registry.get("run_tests")
    assert set(tool.input_model.model_json_schema()["properties"]) == {"target"}
    with pytest.raises(PermissionError):
        await tool.preflight({"target": "unit"}, ToolContext(session_id="s", run_id="r"))


async def test_read_permission_does_not_authorize_test_execution(tmp_path):
    app = make_app(tmp_path, assistant_enabled=True, workspace_allowed_roots=[tmp_path],
                   code_task_sandbox_backend="docker", code_task_docker_image="pytest@sha256:" + "a" * 64)
    async with app.router.lifespan_context(app):
        project = tmp_path / "project"
        project.mkdir()
        (project / "test_demo.py").write_text("def test_demo(): assert True\n")
        workspace = await app.state.workspace_service.register_original(project, ["test_demo.py"])
        context = ToolContext(session_id="s", run_id="missing", workspace_id=workspace.workspace_id,
            profile=CodeTaskCapabilityProfile(allowed_paths=(str(project),), sandbox_backend="docker",
                allowed_tools=frozenset({"run_tests"})))
        with pytest.raises(PermissionError):
            await app.state.task_service.assistant.registry.get("run_tests").preflight({"target": "unit"}, context)


async def test_docker_tracking_schema_persists_and_reopens(tmp_path):
    app = make_app(tmp_path)
    async with app.router.lifespan_context(app):
        import aiosqlite
        async with aiosqlite.connect(app.state.execution_repository.path) as db:
            rows = await db.execute_fetchall("PRAGMA table_info(docker_tool_executions)")
        assert {row[1] for row in rows} >= {"run_id", "invocation_id", "step_id", "container_name", "daemon_id", "launch_closed"}


async def test_tracked_executor_refuses_missing_execution_identity(tmp_path):
    from app.execution.docker import TrackedDockerExecutor
    from app.agent.sandbox_docker import DockerSandboxExecutor
    from app.agent.sandbox import SandboxRequest
    app = make_app(tmp_path)
    executor = TrackedDockerExecutor(DockerSandboxExecutor("pytest@sha256:" + "a" * 64),
        app.state.execution_repository, ToolContext(session_id="s", run_id="r"))
    result = await executor.execute(SandboxRequest(tool_name="run_tests", arguments={}, request_id="req"),
        CodeTaskCapabilityProfile())
    assert result.error_code == "execution_identity_missing"


@pytest.mark.parametrize("exit_code", [0, 1, "cleanup_failed", "backend_unavailable", "timeout", "cancelled"])
async def test_assistant_http_worker_records_docker_tests(tmp_path, monkeypatch, exit_code):
    import httpx
    import aiosqlite
    from app.agent.sandbox_docker import DockerSandboxExecutor
    from test_assistant_api import Provider, decision, request
    app = make_app(tmp_path, assistant_enabled=True, workspace_allowed_roots=[tmp_path],
        code_task_sandbox_backend="docker", code_task_docker_image="pytest@sha256:" + "a" * 64)
    async def query(binary, *args):
        return "trusted-daemon" if "info" in args else ""
    async def execute(executor, request, profile, *, container_name=None):
        assert container_name
        assert profile.sandbox_backend == "docker"
        async with aiosqlite.connect(app.state.execution_repository.path) as db:
            rows = await db.execute_fetchall("SELECT container_name,launch_closed FROM docker_tool_executions")
        assert rows == [(container_name, 0)]
        assert request.arguments["argv"] == ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_demo.py"]
        if isinstance(exit_code, str):
            return SandboxResult(status="timed_out" if exit_code == "timeout" else
                "cancelled" if exit_code == "cancelled" else "failed", error_code=exit_code)
        return SandboxResult(status="completed", exit_code=exit_code, stdout="test report")
    monkeypatch.setattr("app.execution.docker.docker_query", query)
    monkeypatch.setattr(DockerSandboxExecutor, "execute", execute)
    async with app.router.lifespan_context(app):
        app.state.task_service.enabled = app.state.task_service.model_available = True
        project = tmp_path / "project"
        project.mkdir()
        (project / "test_demo.py").write_text("def test_demo(): assert True\n")
        workspace = await app.state.workspace_service.register_original(project, ["test_demo.py"])
        import json
        class TestProvider(Provider):
            async def complete(self, messages, tools):
                if self.calls == 1 and not isinstance(exit_code, str):
                    self.calls += 1
                    observed = json.loads(messages[-1].content)["assistant_tool_result"]["result"]
                    return decision("finish", {"result": observed})
                return await super().complete(messages, tools)
        provider = TestProvider([decision("call_tool", {"name": "run_tests", "contract_version": "1",
            "arguments": {"target": "unit"}}), decision("finish", {"result": {"answer": "Tests executed"}})])
        app.state.task_service.assistant.llm = provider
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            session = (await client.post("/api/sessions", json={})).json()["session_id"]
            created = await client.post(f"/api/sessions/{session}/tasks", json=request(
                workspace_id=workspace.workspace_id, options={"allow_test_execution": True}))
            assert created.status_code == 202, created.text
            run_id = created.json()["run_id"]
            assert await app.state.execution_worker.run_once()
            snapshot = await app.state.execution_repository.get_snapshot(run_id)
            if isinstance(exit_code, str):
                assert snapshot.test_results and snapshot.test_results[0]["passed"] is False
                final = (await client.get(f"/api/tasks/{run_id}")).json()
                assert final["status"] == "waiting"
                return
            assert snapshot.test_results and snapshot.test_results[0]["passed"] == (exit_code == 0)
            assert snapshot.artifact_ids
            final = (await client.get(f"/api/tasks/{run_id}")).json()
            assert final["status"] == "completed", final
            assert final["result"]["test_results"] == snapshot.test_results
            assert final["result"]["artifacts"]
            evidence = await app.state.task_service.assistant.verify_test_termination(run_id,
                (await app.state.execution_repository.get_snapshot(run_id)).harness_state["structured_state"][
                    "capability_feedback"]["result"]["test_results"][0]["invocation_id"])
            assert evidence["source"] == "docker_daemon"
            async with aiosqlite.connect(app.state.execution_repository.path) as db:
                invocation = snapshot.test_results[0]["invocation_id"]
                await db.execute("INSERT INTO assistant_effect_settlements(invocation_id,resolution) VALUES (?,?)",
                    (invocation, "executed"))
                await db.commit()
            await app.state.task_service.assistant.reconcile_test_quarantine(run_id, invocation)
            settlements = await app.state.execution_repository.list_effect_settlements(run_id)
            assert settlements[0]["quarantined"] is False
            assert settlements[0]["termination_evidence"]


@pytest.mark.parametrize("closed,daemon,present,expected", [
    (0, "trusted", "", False), (1, "other", "", False), (1, None, "", False),
    (1, "trusted", "container", False), (1, "trusted", None, False), (1, "trusted", "", True),
])
async def test_verifier_reopens_database_and_requires_closed_launch(tmp_path, monkeypatch, closed, daemon, present, expected):
    import aiosqlite
    from uuid import uuid4
    from app.execution.models import OperationInput
    from app.execution.repository import ExecutionRepository
    from app.execution.docker import DockerTerminationVerifier
    from test_execution_recovery import setup_repository, snapshot
    repo, run, lease, _ = await setup_repository(tmp_path)
    op = OperationInput(id=uuid4(), logical_index=0, kind="tool", tool_name="run_tests",
        input_payload={"target": "unit"}, recovery_class="sandbox")
    await repo.prepare_operation(lease, op, snapshot(run.id))
    step = await repo.start_step(lease, op.id, snapshot(run.id))
    async with aiosqlite.connect(repo.path) as db:
        await db.execute("INSERT INTO docker_tool_executions "
            "(step_id,run_id,invocation_id,container_name,daemon_id,launch_closed) VALUES (?,?,?,?,?,?)",
            (str(step.id), str(run.id), str(op.id), "container", "trusted", closed))
        await db.commit()
    calls = []
    async def query(binary, *args):
        calls.append(args)
        return daemon if "info" in args else present
    monkeypatch.setattr("app.execution.docker.docker_query", query)
    evidence = await DockerTerminationVerifier(ExecutionRepository(repo.path))(run.id, op.id)
    assert bool(evidence) == expected
    if not closed:
        assert not calls


def test_completion_accepts_only_observed_test_reports():
    from types import SimpleNamespace
    from app.assistant.completion import validate_completion
    report = {"target": "unit", "passed": True}
    snapshot = SimpleNamespace(evidence_refs=[], test_results=[report], artifact_ids=[])
    result = validate_completion(snapshot, {"answer": "passed", "validation_status": "passed", "test_results": [report]})
    assert result.test_results == [report]
    with pytest.raises(ValueError):
        validate_completion(snapshot, {"validation_status": "passed", "test_results": [{"target": "fake", "passed": True}]})


async def test_duplicate_step_cannot_launch_again_after_restart(tmp_path, monkeypatch):
    from uuid import uuid4
    from app.execution.models import OperationInput
    from app.execution.repository import ExecutionRepository
    from app.execution.docker import TrackedDockerExecutor, DockerTerminationVerifier
    from app.agent.sandbox import SandboxRequest
    from test_execution_recovery import setup_repository, snapshot
    repo, run, lease, _ = await setup_repository(tmp_path)
    op = OperationInput(id=uuid4(), logical_index=0, kind="tool", tool_name="run_tests",
        input_payload={}, recovery_class="sandbox")
    await repo.prepare_operation(lease, op, snapshot(run.id))
    step = await repo.start_step(lease, op.id, snapshot(run.id))
    context = ToolContext(session_id="s", run_id=str(run.id), invocation_id=str(op.id), step_id=str(step.id), lease=lease)
    launched = []
    class Executor:
        docker_binary = "docker"
        async def execute(self, request, profile, *, container_name):
            launched.append(container_name)
            return SandboxResult(status="completed", exit_code=0)
    async def query(binary, *args):
        return "trusted" if "info" in args else ""
    monkeypatch.setattr("app.execution.docker.docker_query", query)
    assert await DockerTerminationVerifier(repo)(run.id, op.id) is None
    request = SandboxRequest(tool_name="run_tests", request_id="req", arguments={})
    assert (await TrackedDockerExecutor(Executor(), repo, context).execute(request, CodeTaskCapabilityProfile())).status == "completed"
    import aiosqlite
    with pytest.raises(aiosqlite.IntegrityError):
        await TrackedDockerExecutor(Executor(), ExecutionRepository(repo.path, clock=repo.clock), context).execute(request, CodeTaskCapabilityProfile())
    assert len(launched) == 1
    from datetime import timedelta
    from app.execution.state_machine import StaleExecutionOwner
    repo.clock.value += timedelta(seconds=31)
    with pytest.raises(StaleExecutionOwner):
        await TrackedDockerExecutor(Executor(), repo, context).execute(request, CodeTaskCapabilityProfile())
    assert len(launched) == 1


@pytest.mark.parametrize("value", ["true", 1, None])
def test_execution_authorization_is_strict_boolean(value):
    from app.tasks.models import UnifiedTaskRequest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        UnifiedTaskRequest(kind="assistant", message="test", client_request_id="r", options={"allow_test_execution": value})