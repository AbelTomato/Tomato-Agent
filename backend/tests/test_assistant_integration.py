import json
import asyncio
from pathlib import Path

import aiosqlite
import httpx
import pytest

from app.agent.models import LLMResponse
from app.knowledge.ingestion import ingest_manifest
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.service import KnowledgeService
from test_assistant_api import Provider, assistant_api, register, request
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
        from app.memory.models import MemoryItemCreate, MemorySourceInput

        repo = await seed(app, rag_database_path)
        citations = (await KnowledgeService(repo).retrieve("SETEX")).citations
        assert citations
        assistant = app.state.task_service.assistant
        assistant.config.memory_enabled = True
        assistant.layered_memory_service = None  # Exercise the frozen legacy facts path.
        memory = await app.state.dependencies.memory_repository.create(MemoryItemCreate(
            kind="preference", scope="personal", scope_id=assistant.config.desktop_environment_id,
            topic_key="response.style", content="回答时偏好简洁说明",
            sources=[MemorySourceInput(
                source_system="tomato-desktop", source_installation_id="install-1",
                session_id=session, message_id="style-source", excerpt="请简洁一些",
            )],
        ), request_id="knowledge-with-memory")
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
        first_prompt = "\n".join(item.content for item in provider.messages[0] if item.role == "system")
        assert f"memory_id={memory.item_id}" in first_prompt
        assert memory.item_id not in json.dumps(final["result"]["citations"])
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


async def test_python_agent_gets_ephemeral_memory_without_persisting_memory_text(tmp_path):
    from app.memory.models import MemoryItemCreate, MemorySourceInput

    async with assistant_api(tmp_path, [decision("finish", {"result": {"answer": "Used your preference."}})]) as (app, client, session, provider):
        assistant = app.state.task_service.assistant
        assistant.config.memory_enabled = True
        assistant.layered_memory_service = None  # Exercise the frozen legacy facts path.
        new_session = (await client.post("/api/sessions", json={})).json()["session_id"]
        memory = await app.state.dependencies.memory_repository.create(MemoryItemCreate(
            kind="preference", scope="personal", scope_id=assistant.config.desktop_environment_id,
            topic_key="response.language", content="默认用简体中文回答",
            sources=[MemorySourceInput(
                source_system="tomato-desktop", source_installation_id="install-1",
                session_id=session, message_id="memory-source", excerpt="我偏好简体中文",
            )],
        ), request_id="assistant-memory")

        run_id = (await client.post(f"/api/sessions/{new_session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()

        system_prompt = "\n".join(item.content for item in provider.messages[0] if item.role == "system")
        assert "<Relevant Memory data=\"untrusted\">" in system_prompt
        assert f"memory_id={memory.item_id} version=1" in system_prompt
        assert "默认用简体中文回答" in system_prompt
        assert "记忆 ID" in system_prompt
        assert "当前请求优先" in system_prompt
        snapshot = await app.state.execution_repository.get_snapshot(run_id)
        persisted = json.dumps({
            "messages": [item.model_dump(mode="json") for item in snapshot.messages],
            "harness_state": snapshot.harness_state,
        }, ensure_ascii=False)
        assert "默认用简体中文回答" not in persisted
        assert snapshot.reserved_model_calls == 1
        traces = await app.state.dependencies.memory_repository.list_contexts(run_id=run_id)
        assert len(traces) == 1
        assert traces[0]["items"][0]["selection_reason"] == "core_preference"
        assert traces[0]["items"][0]["used_tokens"] > 0
        assert traces[0]["query_hash"] != "Explain the project"
        traced = await client.get(f"/api/memory/contexts/{run_id}")
        assert traced.status_code == 200
        assert traced.json()["contexts"][0]["items"][0]["item_id"] == memory.item_id


async def test_python_agent_retries_if_memory_is_forgotten_during_provider_call(tmp_path):
    from app.memory.models import MemoryItemCreate, MemorySourceInput

    responses = [decision("finish", {"result": {"answer": "Stale answer."}}),
                 decision("finish", {"result": {"answer": "Fresh answer."}})]
    async with assistant_api(tmp_path, responses) as (app, client, session, provider):
        assistant = app.state.task_service.assistant
        assistant.config.memory_enabled = True
        assistant.layered_memory_service = None  # Exercise the frozen legacy facts path.
        memory = await app.state.dependencies.memory_repository.create(MemoryItemCreate(
            kind="background", scope="personal", scope_id=assistant.config.desktop_environment_id,
            topic_key="work.language", content="Explain the project with Python backend experience",
            sources=[MemorySourceInput(
                source_system="tomato-desktop", source_installation_id="install-1",
                session_id=session, message_id="memory-source", excerpt="我主要用 Python",
            )],
        ), request_id="assistant-memory-race")
        original_complete = provider.complete
        forgotten = False

        async def forget_during_provider(messages, tools, *, response_format=None):
            nonlocal forgotten
            if not forgotten:
                forgotten = True
                await app.state.dependencies.memory_repository.forget(
                    memory.item_id, expected_version=1, request_id="forget-during-model-call",
                )
            return await original_complete(messages, tools, response_format=response_format)

        provider.complete = forget_during_provider
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()

        final = (await client.get(f"/api/tasks/{run_id}")).json()
        assert final["status"] == "completed"
        assert final["result"]["answer"] == "Fresh answer."
        assert provider.calls == 2
        first_prompt = "\n".join(item.content for item in provider.messages[0] if item.role == "system")
        second_prompt = "\n".join(item.content for item in provider.messages[1] if item.role == "system")
        assert "Explain the project with Python backend experience" in first_prompt
        assert "Explain the project with Python backend experience" not in second_prompt
        snapshot = await app.state.execution_repository.get_snapshot(run_id)
        assert snapshot.reserved_model_calls == 2


async def test_python_agent_keeps_frozen_legacy_memory_tools_off_layered_path(tmp_path):
    from app.errors import ToolNotFoundError

    async with assistant_api(
        tmp_path, [decision("finish", {"result": {"answer": "普通对话继续使用分层记忆。"}})],
        memory_enabled=True,
    ) as (app, _client, _session, _provider):
        assistant = app.state.task_service.assistant
        assert assistant.layered_memory_service is not None
        for name in ("search_memory", "read_memory"):
            with pytest.raises(ToolNotFoundError):
                assistant.registry.resolve(name)


async def test_python_agent_revalidates_memory_after_user_pause_and_forget(tmp_path):
    from app.memory.models import MemoryItemCreate, MemorySourceInput

    responses = [
        decision("ask_user", {"question": "Which format should I use?"}),
        decision("finish", {"result": {"answer": "I will follow your current choice."}}),
    ]
    async with assistant_api(tmp_path, responses, memory_enabled=True) as (app, client, session, provider):
        assistant = app.state.task_service.assistant
        assistant.layered_memory_service = None  # Exercise the frozen legacy facts path.
        memory = await app.state.dependencies.memory_repository.create(MemoryItemCreate(
            kind="preference", scope="personal", scope_id=assistant.config.desktop_environment_id,
            topic_key="response.style", content="通常偏好用项目符号回答",
            sources=[MemorySourceInput(
                source_system="tomato-desktop", source_installation_id="install-1",
                session_id=session, message_id="pause-memory-source", excerpt="请用项目符号",
            )],
        ), request_id="assistant-memory-pause")
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        waiting = (await client.get(f"/api/tasks/{run_id}")).json()
        assert waiting["status"] == "waiting"
        assert "通常偏好用项目符号回答" in "\n".join(
            item.content for item in provider.messages[0] if item.role == "system")

        await app.state.dependencies.memory_repository.forget(
            memory.item_id, expected_version=1, request_id="forget-while-paused",
        )
        action = {"action": "provide_input", "action_id": waiting["pending_action"]["action_id"],
                  "expected_version": waiting["version"], "client_request_id": "resume-with-current-choice",
                  "payload": {"message": "Use a short paragraph."}}
        assert (await client.post(f"/api/tasks/{run_id}/actions", json=action)).status_code == 200
        assert await app.state.execution_worker.run_once()

        final = (await client.get(f"/api/tasks/{run_id}")).json()
        assert final["status"] == "completed"
        resumed_prompt = "\n".join(item.content for item in provider.messages[1] if item.role == "system")
        assert "通常偏好用项目符号回答" not in resumed_prompt
        traces = await app.state.dependencies.memory_repository.list_contexts(run_id=run_id)
        assert all(not trace["items"] for trace in traces)


async def test_python_agent_restart_reloads_memory_after_paused_fact_is_forgotten(tmp_path):
    from app.memory.models import MemoryItemCreate, MemorySourceInput
    from test_application_lifecycle import make_app

    first = make_app(tmp_path, assistant_enabled=True, memory_enabled=True)
    async with first.router.lifespan_context(first):
        first.state.task_service.enabled = True
        first.state.task_service.model_available = True
        first_provider = Provider([decision("ask_user", {"question": "Which format should I use?"})])
        first.state.task_service.assistant.llm = first_provider
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first), base_url="http://test",
        ) as client:
            session = (await client.post("/api/sessions", json={})).json()["session_id"]
            assistant = first.state.task_service.assistant
            assistant.layered_memory_service = None  # Exercise the frozen legacy facts path.
            memory = await first.state.memory_repository.create(MemoryItemCreate(
                kind="preference", scope="personal", scope_id=assistant.config.desktop_environment_id,
                topic_key="response.style", content="通常偏好用项目符号回答",
                sources=[MemorySourceInput(
                    source_system="tomato-desktop", source_installation_id="install-1",
                    session_id=session, message_id="restart-memory-source", excerpt="请用项目符号",
                )],
            ), request_id="restart-memory-create")
            created = await client.post(f"/api/sessions/{session}/tasks", json=request())
            assert created.status_code == 202, created.text
            run_id = created.json()["run_id"]
            assert await first.state.execution_worker.run_once()
            waiting = (await client.get(f"/api/tasks/{run_id}")).json()
            assert waiting["status"] == "waiting"
            first_prompt = "\n".join(
                item.content for item in first_provider.messages[0] if item.role == "system")
            assert memory.content in first_prompt

    second = make_app(tmp_path, assistant_enabled=True, memory_enabled=True)
    async with second.router.lifespan_context(second):
        second.state.task_service.enabled = True
        second.state.task_service.model_available = True
        second.state.task_service.assistant.layered_memory_service = None
        await second.state.memory_repository.forget(
            memory.item_id, expected_version=memory.version, request_id="restart-memory-forget",
        )
        second_provider = Provider([
            decision("finish", {"result": {"answer": "I will use the format you just chose."}}),
        ])
        second.state.task_service.assistant.llm = second_provider
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second), base_url="http://test",
        ) as client:
            waiting = (await client.get(f"/api/tasks/{run_id}")).json()
            assert waiting["status"] == "waiting"
            action = {
                "action": "provide_input",
                "action_id": waiting["pending_action"]["action_id"],
                "expected_version": waiting["version"],
                "client_request_id": "restart-current-choice",
                "payload": {"message": "Use a short paragraph."},
            }
            assert (await client.post(f"/api/tasks/{run_id}/actions", json=action)).status_code == 200
            assert await second.state.execution_worker.run_once()
            final = (await client.get(f"/api/tasks/{run_id}")).json()
            assert final["status"] == "completed"
            assert second_provider.calls == 1
            resumed_prompt = "\n".join(
                item.content for item in second_provider.messages[0] if item.role == "system")
            assert memory.content not in resumed_prompt
            snapshot = await second.state.execution_repository.get_snapshot(run_id)
            persisted = json.dumps({
                "messages": [item.model_dump(mode="json") for item in snapshot.messages],
                "harness_state": snapshot.harness_state,
            }, ensure_ascii=False)
            assert memory.content not in persisted
            traces = await second.state.memory_repository.list_contexts(run_id=run_id)
            assert all(not trace["items"] for trace in traces)


async def test_python_agent_uses_fresh_profile_projection_and_traces_its_fact_dependencies(tmp_path):
    from app.memory.models import MemoryItemCreate, MemorySourceInput

    async with assistant_api(
        tmp_path, [decision("finish", {"result": {"answer": "Understood."}})], memory_enabled=True,
    ) as (app, client, session, provider):
        assistant = app.state.task_service.assistant
        assistant.layered_memory_service = None  # Exercise the frozen legacy projection path.
        first_memory = await app.state.memory_repository.create(MemoryItemCreate(
            kind="preference", scope="personal", scope_id=assistant.config.desktop_environment_id,
            topic_key="response.style",
            content="内容较长、步骤较多、选项较多或需要比较几个方案时，偏好用项目符号分别列出各部分，方便逐项阅读和快速定位信息。",
            sources=[MemorySourceInput(
                source_system="tomato-desktop", source_installation_id="install-1",
                session_id=session, message_id="profile-summary-source", excerpt="请用项目符号",
            )],
        ), request_id="assistant-profile-summary")
        second_memory = await app.state.memory_repository.create(MemoryItemCreate(
            kind="preference", scope="personal", scope_id=assistant.config.desktop_environment_id,
            topic_key="interaction.tradeoffs",
            content="比较多个方案时，希望逐项列出优缺点、重要取舍和适用条件。",
            sources=[MemorySourceInput(
                source_system="tomato-desktop", source_installation_id="install-1",
                session_id=session, message_id="profile-tradeoffs-source", excerpt="列出取舍",
            )],
        ), request_id="assistant-profile-summary-tradeoffs")
        await app.state.memory_repository.save_projection(
            scope="personal", scope_id=assistant.config.desktop_environment_id,
            projection_type="scope_summary", projection_key="profile",
            content="较长或复杂回答偏好用项目符号分项组织；方案比较时逐项列出优缺点、取舍和适用条件。",
            expected_generation=await app.state.memory_repository.generation(),
            dependencies=[
                {"item_id": first_memory.item_id, "version": first_memory.version},
                {"item_id": second_memory.item_id, "version": second_memory.version},
            ],
        )

        run_id = (await client.post(
            f"/api/sessions/{session}/tasks", json=request(),
        )).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        prompt = "\n".join(item.content for item in provider.messages[0] if item.role == "system")
        assert "方案比较时逐项列出优缺点" in prompt
        assert first_memory.content not in prompt
        assert second_memory.content not in prompt
        traces = await app.state.memory_repository.list_contexts(run_id=run_id)
        assert len(traces) == 1
        assert {item["item_id"] for item in traces[0]["items"]} == {
            first_memory.item_id, second_memory.item_id,
        }
        assert all(item["selection_reason"] == "profile_summary_dependency"
                   and item["source_available"] for item in traces[0]["items"])
        assert all(item["used_chars"] > 0 and item["used_tokens"] > 0
                   for item in traces[0]["items"])


async def test_python_agent_keeps_facts_when_profile_projection_increases_context_cost(tmp_path):
    from app.memory.models import MemoryItemCreate, MemorySourceInput

    async with assistant_api(
        tmp_path, [decision("finish", {"result": {"answer": "Understood."}})], memory_enabled=True,
    ) as (app, client, session, provider):
        assistant = app.state.task_service.assistant
        assistant.layered_memory_service = None  # Exercise the frozen legacy projection path.
        memory = await app.state.memory_repository.create(MemoryItemCreate(
            kind="preference", scope="personal", scope_id=assistant.config.desktop_environment_id,
            topic_key="response.style", content="偏好简短回答",
            sources=[MemorySourceInput(
                source_system="tomato-desktop", source_installation_id="install-1",
                session_id=session, message_id="short-profile-source", excerpt="偏好简短回答",
            )],
        ), request_id="assistant-profile-short-fact")
        await app.state.memory_repository.save_projection(
            scope="personal", scope_id=assistant.config.desktop_environment_id,
            projection_type="scope_summary", projection_key="profile",
            content="用户偏好使用简短的回答方式。",
            expected_generation=await app.state.memory_repository.generation(),
            dependencies=[{"item_id": memory.item_id, "version": memory.version}],
        )

        run_id = (await client.post(
            f"/api/sessions/{session}/tasks", json=request(),
        )).json()["run_id"]
        assert await app.state.execution_worker.run_once()

        prompt = "\n".join(item.content for item in provider.messages[0] if item.role == "system")
        assert memory.content in prompt
        assert "用户偏好使用简短的回答方式。" not in prompt
        traces = await app.state.memory_repository.list_contexts(run_id=run_id)
        assert traces[0]["items"][0]["selection_reason"] == "core_preference"


async def test_python_agent_injects_layered_profile_and_project_episodes(tmp_path):
    async with assistant_api(
        tmp_path, [decision("finish", {"result": {"answer": "已按你的偏好处理。"}})],
        memory_enabled=True,
    ) as (app, client, session, provider):
        assistant = app.state.task_service.assistant
        layered = assistant.layered_memory_service
        class FakeEmbedding:
            async def embed(self, texts):
                return [[1.0, 0.0] for _ in texts]
        layered.embedding_client = FakeEmbedding()
        layered.embedding_model = "test-model"
        repository = app.state.dependencies.layered_memory_repository
        await repository.save_profile("用户主要开发 Tomato Agent；偏好 Allman 风格和多行函数。", [])
        await repository.save_episodes(
            batch_key="project-episode",
            episodes=[{"content": "Tomato Agent 当前工作是完成分层记忆 MVP。",
                       "occurred_at": "2026-11-11T00:00:00Z", "source_refs": ["dev-log#t7"]}],
            source_ref="dev-log", vectors=[[1.0, 0.0]], embedding_model="test-model",
            project_key=None,
        )
        run_id = (await client.post(
            f"/api/sessions/{session}/tasks", json=request(message="Tomato Agent 当前要推进什么？"),
        )).json()["run_id"]
        assert await app.state.execution_worker.run_once()

        prompt = "\n".join(item.content for item in provider.messages[0] if item.role == "system")
        assert "Allman 风格和多行函数" in prompt
        assert "完成分层记忆 MVP" in prompt
        snapshot = await app.state.execution_repository.get_snapshot(run_id)
        persisted = json.dumps({
            "messages": [item.model_dump(mode="json") for item in snapshot.messages],
            "harness_state": snapshot.harness_state,
        }, ensure_ascii=False)
        assert "Allman 风格" not in persisted

async def test_layered_memory_profile_is_injected_without_embedding_provider(tmp_path):
    async with assistant_api(
        tmp_path, [decision("finish", {"result": {"answer": "好。"}})], memory_enabled=True,
    ) as (app, client, session, provider):
        await app.state.layered_memory_repository.save_profile("偏好直接回答并使用多行函数。", [])
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        prompt = "\n".join(item.content for item in provider.messages[0] if item.role == "system")
        assert "偏好直接回答并使用多行函数" in prompt
        assert (await client.get(f"/api/tasks/{run_id}")).json()["status"] == "completed"
