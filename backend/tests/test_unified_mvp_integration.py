"""Unified HTTP contracts exercised through the real single Worker and adapters."""

from pathlib import Path
from uuid import uuid4

import httpx
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
import pytest

from app.application import create_app
from app.agent.code_task import CodeTaskStrategy
from app.agent.models import LLMResponse
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.service import KnowledgeService
from app.settings import Settings
from test_original_code_task import Model, Sandbox, call
from test_task_adapters import writing_setup
from test_writing_full_flow import rag_database_path


async def harness(tmp_path, rag_database_path):
    deps, session, service, writer = await writing_setup(tmp_path, rag_database_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("value = 0\n")
    (project / "test_app.py").write_text("def test_value():\n    assert True\n")
    (project / "notes.txt").write_text("pre-existing user changes\n")
    from app.workspaces.service import WorkspaceService
    from app.agent.policies import CodeTaskCapabilityProfile
    workspace_service = WorkspaceService(tmp_path / "original-metadata", allowed_roots=[project])
    deps.workspace_service = workspace_service
    code = deps.code_task_service
    code.run_repository = deps.run_repository
    code.execution_repository = deps.execution_repository
    code.test_tool.runs = deps.run_repository
    async def observe(candidate):
        return code.recovery_observation(await deps.run_repository.get_run(candidate.run_id))
    deps.execution_worker.recovery_observation_factory = observe
    code.workspace_service = workspace_service
    code.artifact_service.workspace_service = workspace_service
    code.test_tool.workspaces = workspace_service
    code.test_tool.executor = Sandbox()
    code.capability_profile_factory = lambda root: CodeTaskCapabilityProfile(
        allowed_paths=(str(root),), sandbox_backend="docker")
    code.strategy = CodeTaskStrategy()
    deps.task_service.adapters["chat"].runtime.loop.llm = Model([
        LLMResponse(kind="final", content="hello back")])
    deps.task_service.adapters["chat"].sessions = deps.session_repository
    deps.task_service.adapters["chat"].runtime.repository = deps.session_repository
    conversation = deps.task_service.adapters["knowledge"].conversation
    conversation.repository = deps.session_repository
    conversation.service = KnowledgeService(KnowledgeRepository(rag_database_path), pipeline=None)
    conversation.llm_client = Model([LLMResponse(kind="final", content='{"query":"SETEX"}'),
        LLMResponse(kind="final", content='{"answer":"SETEX sets expiration.","evidence_status":"supported","citation_ids":["R1"]}')])
    app = create_app(Settings(_env_file=None, database_path=tmp_path / "unused.db", llm_api_key=""))
    for name, value in vars(deps).items():
        setattr(app.state, name, value)
    app.state.writing_repository = service.repository
    return app, deps, str(session), project, writer


async def submit(client, session, kind, message, **values):
    response = await client.post(f"/api/sessions/{session}/tasks", json={
        "kind": kind, "message": message, "client_request_id": str(uuid4()), **values})
    assert response.status_code == 202, response.text
    return response.json()


async def advance(client, deps, run):
    assert await deps.execution_worker.run_once()
    response = await client.get(f"/api/tasks/{run['run_id']}")
    assert response.status_code == 200
    return response.json()


async def act(client, run, name, **payload):
    response = await client.post(f"/api/tasks/{run['run_id']}/actions", json={
        "action": name, "action_id": run["pending_action"]["action_id"],
        "expected_version": run["version"], "client_request_id": str(uuid4()), "payload": payload})
    assert response.status_code == 200, response.text
    return response.json()


async def test_http_worker_four_modes_two_original_rounds(tmp_path, rag_database_path):
    app, deps, session, project, writer = await harness(tmp_path, rag_database_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        chat = await advance(client, deps, await submit(client, session, "chat", "hello"))
        assert chat["status"] == "completed" and chat["result"]["answer"] == "hello back"
        knowledge = await advance(client, deps, await submit(client, session, "knowledge", "SETEX"))
        assert knowledge["status"] == "completed" and knowledge["result"]["citations"]
        writing = await advance(client, deps, await submit(client, session, "writing", "SETEX expiration"))
        assert writing["pending_action"]["reason"] == "confirm_outline"
        outline = writing["result"]["outline"] | {"title": "Edited SETEX"}
        writing = await advance(client, deps, await act(client, writing, "confirm_outline", outline=outline))
        assert writing["pending_action"]["reason"] == "save_draft"
        writing = await advance(client, deps, await act(client, writing, "save_draft"))
        assert writing["status"] == "completed" and Path(writing["result"]["saved_path"]).is_file()
        assert writer.outline_calls == writer.draft_calls == 1
        registered = await client.post("/api/workspaces", json={"path": str(project), "pytest_paths": ["test_app.py"]})
        assert registered.status_code == 201, registered.text
        workspace_id = registered.json()["workspace_id"]
        for value in (1, 2):
            deps.code_task_service.llm = Model([
                call("read_file", {"path": "app.py"}),
                call("write_file", {"path": "app.py", "content": f"value = {value}\n"}),
                call("run_tests", {"target": "unit"}), call("get_diff", {}),
                LLMResponse(kind="final", content="done")])
            run = await advance(client, deps, await submit(client, session, "code", "update value", workspace_id=workspace_id))
            assert run["status"] == "completed", run
            assert run["result"]["test_results"][-1]["passed"] is True
            diff = next(item for item in run["result"]["artifacts"] if item["kind"] == "diff")
            artifact = await client.get(f"/api/code-tasks/{run['run_id']}/artifacts/{diff['artifact_id']}")
            text = artifact.json()["content"]
            assert f"-value = {value - 1}" in text and f"+value = {value}" in text
            assert "notes.txt" not in text
            assert (project / "notes.txt").read_text() == "pre-existing user changes\n"
        history = (await client.get(f"/api/sessions/{session}/tasks")).json()
        unified = [item for item in history["tasks"] if not item["legacy"]]
        assert len(unified) == 5
        assert [item["kind"] for item in unified] == ["chat", "knowledge", "writing", "code", "code"]


@pytest.mark.parametrize("scenario", ["code_write", "generating", "published", "outline", "saved", "unknown_save"])
async def test_independent_process_stage_crash_no_repeated_effects(tmp_path, rag_database_path, scenario):
    app, deps, session, project, writer = await harness(tmp_path, rag_database_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        if scenario == "code_write":
            registered = await client.post("/api/workspaces", json={"path": str(project), "pytest_paths": ["test_app.py"]})
            run = await submit(client, session, "code", "update", workspace_id=registered.json()["workspace_id"])
        elif scenario == "generating":
            run = await submit(client, session, "chat", "hello")
        else:
            run = await advance(client, deps, await submit(client, session, "writing", "SETEX expiration"))
            if scenario != "published":
                run = await act(client, run, "confirm_outline", outline=run["result"]["outline"])
            if scenario in {"saved", "unknown_save"}:
                run = await advance(client, deps, run)
                run = await act(client, run, "save_draft")
        worker = Path(__file__).parent / "fixtures" / "unified_crash_worker.py"
        process = await asyncio.create_subprocess_exec(sys.executable, str(worker),
            str(deps.execution_repository.path), run["run_id"], scenario,
            str(project / "app.py" if scenario == "code_write" else tmp_path / "drafts"),
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), 15)
            assert process.returncode == 73, stderr.decode()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        class Clock:
            def now(self):
                return datetime.now(timezone.utc) + timedelta(seconds=2)
        deps.execution_repository.clock = Clock()
        await deps.execution_worker.recover_on_startup()
        current = (await client.get(f"/api/tasks/{run['run_id']}")).json()
        if scenario == "code_write":
            assert current["status"] == "waiting"
            assert current["pending_action"]["reason"] == "recovery_required"
            assert (project / "app.py").read_text() == "value = 99\n"
        elif scenario in {"generating", "outline"}:
            assert current["status"] == "failed" and current["error"]["code"] == "interrupted"
        elif scenario == "published":
            assert current["status"] == "waiting"
            assert current["pending_action"]["reason"] == "confirm_outline"
        elif scenario == "saved":
            assert current["status"] == "completed"
            assert len(list((tmp_path / "drafts").glob("*.md"))) == 1
        else:
            assert current["status"] == "waiting"
            assert current["pending_action"]["reason"] == "save_outcome_unknown"
            assert (tmp_path / "drafts" / "unknown.md").read_text() == "unconfirmed partial write\n"
        assert not await deps.execution_worker.run_once()
        assert writer.outline_calls == (0 if scenario in {"generating", "code_write"} else 1)
        assert writer.draft_calls == (1 if scenario in {"saved", "unknown_save"} else 0)


async def test_http_empty_evidence_and_original_conflict(tmp_path, rag_database_path):
    app, deps, session, project, writer = await harness(tmp_path, rag_database_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        conversation = deps.task_service.adapters["knowledge"].conversation
        conversation.llm_client = Model([])
        empty = await advance(client, deps, await submit(client, session, "knowledge", "missing-unicorn"))
        assert empty["status"] == "completed"
        assert empty["result"]["evidence_status"] == "no_results" and empty["result"]["citations"] == []
        registered = await client.post("/api/workspaces", json={"path": str(project), "pytest_paths": ["test_app.py"]})
        model = Model([call("read_file", {"path": "app.py"}),
            call("write_file", {"path": "app.py", "content": "value = 1\n"}),
            call("run_tests", {"target": "unit"}), call("get_diff", {}),
            LLMResponse(kind="final", content="done")])
        original = model.complete
        async def complete(messages, tools):
            response = await original(messages, tools)
            if response.kind == "tool_call" and response.tool_call.name == "write_file":
                (project / "app.py").write_text("external editor content\n")
            return response
        model.complete = complete
        deps.code_task_service.llm = model
        run = await advance(client, deps, await submit(client, session, "code", "update", workspace_id=registered.json()["workspace_id"]))
        assert run["status"] == "failed"
        assert (project / "app.py").read_text() == "external editor content\n"
        assert (project / "notes.txt").read_text() == "pre-existing user changes\n"


@pytest.mark.parametrize("status,exit_code", [("completed", 1), ("failed", None)])
async def test_failed_or_unavailable_test_backend_cannot_complete(tmp_path, rag_database_path, status, exit_code):
    from app.agent.sandbox import SandboxResult
    app, deps, session, project, writer = await harness(tmp_path, rag_database_path)
    class FailingSandbox(Sandbox):
        async def execute(self, request, profile):
            return SandboxResult(status=status, exit_code=exit_code, stdout="tests failed",
                                 error_code="backend_unavailable" if status == "failed" else None)
    deps.code_task_service.test_tool.executor = FailingSandbox()
    deps.code_task_service.llm = Model([call("run_tests", {"target": "unit"}),
        call("get_diff", {}), LLMResponse(kind="final", content="done")])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        registered = await client.post("/api/workspaces", json={"path": str(project), "pytest_paths": ["test_app.py"]})
        run = await advance(client, deps, await submit(client, session, "code", "verify", workspace_id=registered.json()["workspace_id"]))
        assert run["status"] == "failed"
        assert run["result"]["test_results"][-1]["passed"] is not True