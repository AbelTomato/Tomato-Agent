import httpx
import pytest
from uuid import uuid4

from app.application import create_app
from app.agent.models import CodeTaskRequest, LLMResponse, ToolCall
from app.agent.code_task import CodeTaskStrategy
from app.agent.sandbox import SandboxResult
from app.settings import Settings


def make_app(tmp_path):
    return create_app(Settings(
        _env_file=None,
        database_path=tmp_path / "agent.db",
        docs_root=tmp_path,
        code_task_workspace_root=tmp_path / "workspaces",
        code_task_artifact_root=tmp_path / "artifacts",
        llm_api_key="",
        knowledge_pipeline_enabled=False,
        knowledge_rerank_enabled=False,
    ))


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = iter(responses)

    async def complete(self, messages, tools):
        return next(self.responses)


class FakeSandbox:
    def __init__(self, exit_code=0):
        self.exit_code = exit_code

    async def execute(self, request, profile):
        return SandboxResult(
            status="completed", exit_code=self.exit_code,
            stdout="1 failed" if self.exit_code else "1 passed",
        )


def tool(name, arguments):
    return LLMResponse(kind="tool_call", tool_call=ToolCall(name=name, arguments=arguments))


def execution_dependencies(app, *, test_exit_code=0):
    service = app.state.code_task_service
    service.llm = ScriptedLLM([
        tool("apply_patch", {"changes": [{"path": "calculator.py",
            "old_text": "return left - right", "new_text": "return left + right"}]}),
        tool("run_tests", {"target": "unit"}),
        tool("get_diff", {}),
        LLMResponse(kind="final", content="Fixed calculator addition"),
    ])
    service.strategy = CodeTaskStrategy()
    service.test_tool.executor = FakeSandbox(test_exit_code)
    app.state.code_task_llm = service.llm
    app.state.code_task_strategy = service.strategy
    app.state.code_task_test_tool = service.test_tool


@pytest.mark.asyncio
async def test_code_task_api_create_query_events_and_artifact_scope(tmp_path):
    app = make_app(tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post("/api/code-tasks", json={"task": "Fix it"})
            assert created.status_code == 201, created.text
            body = created.json()
            assert body["status"] == "queued"
            run_id = body["run_id"]

            queried = await client.get(f"/api/code-tasks/{run_id}")
            assert queried.status_code == 200
            assert queried.json()["run_id"] == run_id
            assert queried.json()["status"] == "queued"
            assert await app.state.code_task_service.execution_repository.get_snapshot(run_id) is not None

            events = await client.get(f"/api/code-tasks/{run_id}/events")
            assert events.status_code == 200
            assert events.json()["events"][0]["event_type"] == "code_task.created"

            missing = await client.get("/api/code-tasks/not-a-uuid")
            assert missing.status_code == 400

            artifact = await client.get(
                f"/api/code-tasks/{run_id}/artifacts/not-a-uuid"
            )
            assert artifact.status_code == 404


@pytest.mark.asyncio
async def test_code_task_api_rejects_invalid_input_and_maps_unconfigured_execute(tmp_path):
    app = make_app(tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            invalid = await client.post("/api/code-tasks", json={"task": " "})
            assert invalid.status_code == 422

            created = await client.post("/api/code-tasks", json={"task": "Run tests"})
            run_id = created.json()["run_id"]
            executed = await client.post(f"/api/code-tasks/{run_id}/execute")
            assert executed.status_code == 503
            assert executed.json()["detail"]["code"] == "model_unconfigured"


@pytest.mark.asyncio
async def test_code_task_api_cancel_is_idempotent_and_unknown_is_404(tmp_path):
    app = make_app(tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post("/api/code-tasks", json={"task": "Cancel"})
            run_id = created.json()["run_id"]
            first = await client.post(f"/api/code-tasks/{run_id}/cancel", json={})
            assert first.status_code == 200
            assert first.json()["status"] == "cancelled"
            second = await client.post(f"/api/code-tasks/{run_id}/cancel", json={})
            assert second.status_code == 200
            assert second.json()["status"] == "cancelled"
            unknown = await client.post(f"/api/code-tasks/{uuid4()}/cancel", json={})
            assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_code_task_api_executes_fixed_fixture_and_returns_auditable_success(tmp_path):
    app = make_app(tmp_path)
    execution_dependencies(app)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post("/api/code-tasks", json={"task": "Fix addition"})
            run_id = created.json()["run_id"]
            workspace = created.json()["workspace_id"]
            repo = app.state.workspace_service._directory(workspace) / "repo"
            assert {"calculator.py", "test_calculator.py"} == {p.name for p in repo.iterdir()}

            response = await client.post(f"/api/code-tasks/{run_id}/execute")
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["status"] == "completed"
            assert result["changed_files"] == ["calculator.py"]
            assert result["test_results"][-1]["exit_code"] == 0
            assert result["tool_calls"] == 3
            assert result["usage"]["model_calls"] == 4
            artifact_id = result["diff_artifact"]["artifact_id"]
            artifact = await client.get(f"/api/code-tasks/{run_id}/artifacts/{artifact_id}")
            assert artifact.status_code == 200
            assert "return left + right" in artifact.json()["content"]
            assert [event["event_type"] for event in result["events"]][-1] == "code_task.completed"


@pytest.mark.asyncio
async def test_code_task_api_query_restores_complete_result_after_service_restart(tmp_path):
    first_app = make_app(tmp_path)
    execution_dependencies(first_app)
    async with first_app.router.lifespan_context(first_app):
        transport = httpx.ASGITransport(app=first_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post("/api/code-tasks", json={"task": "Fix addition"})
            run_id = created.json()["run_id"]
            executed = await client.post(f"/api/code-tasks/{run_id}/execute")
            assert executed.status_code == 200, executed.text
            expected = executed.json()

    restarted_app = make_app(tmp_path)
    async with restarted_app.router.lifespan_context(restarted_app):
        transport = httpx.ASGITransport(app=restarted_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            queried = await client.get(f"/api/code-tasks/{run_id}")
            assert queried.status_code == 200, queried.text
            restored = queried.json()
            assert restored["status"] == expected["status"] == "completed"
            assert restored["usage"] == expected["usage"]
            assert restored["tool_calls"] == expected["tool_calls"]
            assert restored["test_results"] == expected["test_results"]
            assert restored["diff_artifact"] == expected["diff_artifact"]
            assert restored["changed_files"] == expected["changed_files"]
            assert restored["artifacts"] == expected["artifacts"]

            events = await client.get(f"/api/code-tasks/{run_id}/events")
            assert events.status_code == 200, events.text
            assert events.json()["events"] == expected["events"]

            artifact_id = restored["diff_artifact"]["artifact_id"]
            artifact = await client.get(
                f"/api/code-tasks/{run_id}/artifacts/{artifact_id}"
            )
            assert artifact.status_code == 200, artifact.text
            assert artifact.json()["artifact"] == restored["diff_artifact"]
            assert "return left + right" in artifact.json()["content"]


@pytest.mark.asyncio
async def test_code_task_api_test_failure_never_returns_success(tmp_path):
    app = make_app(tmp_path)
    execution_dependencies(app, test_exit_code=1)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post("/api/code-tasks", json={"task": "Fix addition"})
            run_id = created.json()["run_id"]
            response = await client.post(f"/api/code-tasks/{run_id}/execute")
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "tests_failed"
            assert response.json()["detail"]["result"]["status"] == "failed"


@pytest.mark.asyncio
async def test_code_task_api_rejects_cross_run_artifact_access(tmp_path):
    app = make_app(tmp_path)
    async with app.router.lifespan_context(app):
        service = app.state.code_task_service
        first = await service.create(CodeTaskRequest(task="one"))
        second = await service.create(CodeTaskRequest(task="two"))
        ref = await service.artifact_service.register_text(first.id, "report", "private")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/code-tasks/{second.id}/artifacts/{ref.artifact_id}")
            assert response.status_code == 404


@pytest.mark.asyncio
async def test_code_task_api_rejects_unknown_execute_and_extra_request_fields(tmp_path):
    app = make_app(tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            unknown = await client.post(f"/api/code-tasks/{uuid4()}/execute")
            assert unknown.status_code == 404
            extra = await client.post("/api/code-tasks", json={
                "task": "Fix it", "workspace_root": "/tmp/attacker",
            })
            assert extra.status_code == 422


@pytest.mark.asyncio
async def test_code_task_api_duplicate_execution_is_rejected_after_completion(tmp_path):
    app = make_app(tmp_path)
    execution_dependencies(app)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post("/api/code-tasks", json={"task": "Fix addition"})
            run_id = created.json()["run_id"]
            first = await client.post(f"/api/code-tasks/{run_id}/execute")
            assert first.status_code == 200
            second = await client.post(f"/api/code-tasks/{run_id}/execute")
            assert second.status_code == 409
            assert second.json()["detail"]["code"] == "run_not_active"