import json
from pathlib import Path

import aiosqlite
import pytest

from app.agent.models import LLMResponse
from app.knowledge.ingestion import ingest_manifest
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.service import KnowledgeService
from test_assistant_api import assistant_api, register, request
from test_writing_full_flow import rag_database_path


def decision(kind, payload):
    return LLMResponse(kind="final", content=json.dumps(
        {"protocol_version": 1, "type": kind, "payload": payload}))


async def seed(app, database):
    repo = KnowledgeRepository(database)
    await repo.init()
    fixtures = Path(__file__).parent / "fixtures" / "knowledge"
    report = await ingest_manifest(repo, fixtures / "manifest.json", fixtures)
    assert report.failed == 0
    service = KnowledgeService(repo)
    for name in ("search_knowledge", "answer_knowledge", "read_knowledge"):
        app.state.task_service.assistant.registry.get(name).service = service
    return repo


async def test_same_run_knowledge_project_and_checked_citations(tmp_path, rag_database_path):
    async with assistant_api(tmp_path) as (app, client, session, provider):
        repo = await seed(app, rag_database_path)
        citations = (await KnowledgeService(repo).retrieve("SETEX")).citations
        assert citations
        workspace = await register(app, tmp_path)
        provider.responses = iter([
            decision("call_tool", {"name": "search_knowledge", "contract_version": "1", "arguments": {"query": "SETEX"}}),
            decision("call_tool", {"name": "read_knowledge", "contract_version": "1", "arguments": {"chunk_id": citations[0].chunk_id}}),
            decision("call_tool", {"name": "read_file", "contract_version": "1", "arguments": {"path": "cache.py"}}),
            decision("finish", {"result": {"answer": "SETEX gives the cache a TTL of 60.",
                "citations": [citations[0].model_dump(mode="json")]}}),
        ])
        created = await client.post(f"/api/sessions/{session}/tasks", json=request(workspace_id=workspace.workspace_id))
        assert created.status_code == 202
        run_id = created.json()["run_id"]
        assert await app.state.execution_worker.run_once()
        final = (await client.get(f"/api/tasks/{run_id}")).json()
        assert final["status"] == "completed", final
        assert final["result"]["citations"][0] == citations[0].model_dump(mode="json")
        snapshot = await app.state.execution_repository.get_snapshot(run_id)
        assert snapshot.reserved_model_calls == provider.calls == 4
        assert snapshot.reserved_tool_calls == 3
        assert snapshot.domain_links == {} and snapshot.session_id.hex == session.replace("-", "")
        async with aiosqlite.connect(app.state.execution_repository.path) as db:
            rows = await db.execute_fetchall("SELECT run_id,tool_name,status FROM execution_operations WHERE run_id=?", (run_id,))
        assert {(row[0], row[1], row[2]) for row in rows} == {
            (run_id, name, "succeeded") for name in ("search_knowledge", "read_knowledge", "read_file")}
        assert len((await client.get(f"/api/sessions/{session}/tasks")).json()["tasks"]) == 1


async def test_domain_answer_model_calls_share_ledger(tmp_path, rag_database_path):
    async with assistant_api(tmp_path) as (app, client, session, provider):
        repo = await seed(app, rag_database_path)
        citation = (await KnowledgeService(repo).retrieve("SETEX")).citations[0]
        provider.responses = iter([
            decision("call_tool", {"name": "answer_knowledge", "contract_version": "1", "arguments": {"query": "SETEX"}}),
            LLMResponse(kind="final", content=json.dumps({"answer": "SETEX sets an expiry.",
                "citation_ids": ["R1"], "evidence_status": "supported"})),
            decision("finish", {"result": {"answer": "SETEX sets an expiry.",
                "citations": [citation.model_dump(mode="json")]}}),
        ])
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        final = (await client.get(f"/api/tasks/{run_id}")).json()
        assert final["status"] == "completed", final
        assert final["result"]["citations"]
        calls = await app.state.execution_repository.get_model_calls(run_id)
        assert len(calls) == provider.calls == 3
        assert sum(item["scope"] == "domain" for item in calls) == 1


@pytest.mark.parametrize("response", [
    '{"answer":"fake source","citation_ids":["invented"]}',
    '{"answer":"tests passed","validation_status":"passed"}',
])
async def test_unverified_final_result_is_failed(tmp_path, response):
    payload = {"answer": "fake source", "citation_ids": ["invented"]} if "fake source" in response else {"answer": "tests passed", "validation_status": "passed"}
    async with assistant_api(tmp_path, [decision("finish", {"result": payload})]) as (app, client, session, _):
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        final = (await client.get(f"/api/tasks/{run_id}")).json()
        assert final["status"] == "failed"
        assert final["result"] is None or final["result"]["output"] is None


async def test_clarification_resume_preserves_history_and_budget(tmp_path):
    responses = [decision("ask_user", {"question": "Which topic?"}),
                 decision("finish", {"result": {"answer": "explained"}})]
    async with assistant_api(tmp_path, responses) as (app, client, session, provider):
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        waiting = (await client.get(f"/api/tasks/{run_id}")).json()
        assert waiting["status"] == "waiting"
        action = {"action_id": waiting["pending_action"]["action_id"], "action": "provide_input",
                  "expected_version": waiting["version"], "client_request_id": "input", "payload": {"message": "cache"}}
        assert (await client.post(f"/api/tasks/{run_id}/actions", json=action)).status_code == 200
        assert await app.state.execution_worker.run_once()
        assert "cache" in provider.messages[-1][-1].content
        assert (await app.state.execution_repository.get_snapshot(run_id)).reserved_model_calls == 2


@pytest.mark.parametrize("call", [
    {"name": "write_file", "contract_version": "1", "arguments": {"path": "a.py", "content": "evil"}},
    {"name": "search_knowledge", "contract_version": "1", "arguments": {"query": "ok", "workspace_id": "injected"}},
])
async def test_wrong_tool_or_arguments_are_feedback_without_execution(tmp_path, call):
    responses = [decision("call_tool", call), decision("finish", {"result": {"answer": "Cannot perform that operation."}})]
    async with assistant_api(tmp_path, responses) as (app, client, session, provider):
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        final = (await client.get(f"/api/tasks/{run_id}")).json()
        assert final["status"] == "completed" and provider.calls == 2
        assert (await app.state.execution_repository.get_snapshot(run_id)).reserved_tool_calls == 0


async def test_assistant_startup_recovery_without_workspace(tmp_path):
    from datetime import datetime, timedelta, timezone
    from uuid import UUID
    async with assistant_api(tmp_path, [decision("finish", {"result": {"answer": "recovered"}})]) as (app, client, session, _):
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        repository = app.state.execution_repository
        await repository.claim(UUID(run_id), "crashed", lease_seconds=30)
        async with aiosqlite.connect(repository.path) as db:
            await db.execute("UPDATE execution_attempts SET lease_expires_at=? WHERE run_id=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), run_id))
            await db.commit()
        results = await app.state.execution_worker.recover_on_startup()
        assert len(results) == 1 and results[0].status == "queued"
        assert await app.state.execution_worker.run_once()
        assert (await client.get(f"/api/tasks/{run_id}")).json()["status"] == "completed"


@pytest.mark.parametrize("next_tool", ["read_file", "list_files"])
async def test_rejected_workspace_request_is_not_requested_again(tmp_path, next_tool):
    call = decision("call_tool", {"name": "read_file", "contract_version": "1", "arguments": {"path": "cache.py"}})
    repeated = decision("call_tool", {"name": next_tool, "contract_version": "1", "arguments": {"path": "cache.py"}})
    async with assistant_api(tmp_path, [call, repeated, decision("finish", {"result": {"answer": "Will explain without accessing files."}})]) as (app, client, session, provider):
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        waiting = (await client.get(f"/api/tasks/{run_id}")).json()
        action = {"action": "reject_action", "action_id": waiting["pending_action"]["action_id"],
                  "expected_version": waiting["version"], "client_request_id": "reject", "payload": {}}
        assert (await client.post(f"/api/tasks/{run_id}/actions", json=action)).status_code == 200
        assert await app.state.execution_worker.run_once()
        final = (await client.get(f"/api/tasks/{run_id}")).json()
        assert final["status"] == "completed" and provider.calls == 3
        assert final["workspace_id"] is None


async def test_cancel_waiting_assistant_cannot_resume(tmp_path):
    response = decision("ask_user", {"question": "Which topic?"})
    async with assistant_api(tmp_path, [response]) as (app, client, session, provider):
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        waiting = (await client.get(f"/api/tasks/{run_id}")).json()
        action = {"action": "cancel", "action_id": "cancel", "expected_version": waiting["version"],
                  "client_request_id": "cancel", "payload": {}}
        route = f"/api/tasks/{run_id}/actions"
        assert (await client.post(route, json=action)).json()["status"] == "cancelled"
        assert (await client.post(route, json=action)).status_code == 200
        resume = {**action, "action": "provide_input", "client_request_id": "input", "payload": {"message": "cache"},
                  "action_id": waiting["pending_action"]["action_id"]}
        assert (await client.post(route, json=resume)).status_code == 409
        assert not await app.state.execution_worker.run_once()
        assert provider.calls == 1


async def test_invalid_domain_citations_are_feedback_not_published(tmp_path, rag_database_path):
    async with assistant_api(tmp_path) as (app, client, session, provider):
        await seed(app, rag_database_path)
        provider.responses = iter([
            decision("call_tool", {"name": "answer_knowledge", "contract_version": "1", "arguments": {"query": "SETEX"}}),
            LLMResponse(kind="final", content='{"protocol_version":1,"type":"finish","payload":{"result":{"answer":"fake","citations":[]}}}'),
            decision("finish", {"result": {"answer": "生成的引用未通过校验。"}}),
        ])
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        final = (await client.get(f"/api/tasks/{run_id}")).json()
        assert final["status"] == "completed" and final["result"]["citations"] == []
        assert (await app.state.execution_repository.get_snapshot(run_id)).reserved_model_calls == 3