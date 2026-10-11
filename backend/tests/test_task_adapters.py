from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.tasks.adapters.writing import WritingAdapter
from app.tasks.models import TaskActionRequest
from app.runs.models import RunRepositoryConflict
from test_task_dispatch import setup, request
from test_writing_full_flow import make_flow, rag_database_path


async def writing_setup(tmp_path, rag_database_path, **kwargs):
    deps, session = await setup(tmp_path)
    _, service, executor, llm, task = await make_flow(tmp_path, rag_database_path, **kwargs)
    # Domain and unified repositories must share the same Session database.
    from app.sessions.repository import SessionRepository
    from app.runs.repository import RunRepository
    from app.execution.repository import ExecutionRepository
    deps.session_repository = SessionRepository(service.repository.path)
    deps.run_repository = RunRepository(service.repository.path)
    deps.execution_repository = ExecutionRepository(service.repository.path)
    await deps.run_repository.init()
    await deps.execution_repository.init()
    deps.task_service.runs = deps.run_repository
    deps.task_service.sessions = deps.session_repository
    deps.task_service.execution = deps.execution_repository
    deps.execution_worker.repository = deps.execution_repository
    session = task.session_id
    adapter = WritingAdapter(service, executor, deps.execution_repository)
    deps.task_service.adapters["writing"] = adapter
    return deps, session, service, llm


def action(run, name, key=None, **payload):
    return TaskActionRequest(action=name, action_id=run.pending_action.action_id,
        expected_version=run.version, client_request_id=key or str(uuid4()), payload=payload)


async def test_writing_background_confirm_draft_save_and_idempotency(tmp_path, rag_database_path):
    deps, session, service, llm = await writing_setup(tmp_path, rag_database_path)
    run = await deps.task_service.submit(session, request("writing").model_copy(update={"message": "SETEX expiration"}))
    await deps.execution_worker.run_once()
    run = await deps.run_repository.get_run(run.id)
    assert run.status == "waiting" and run.pending_action.reason == "confirm_outline"
    domain = await service.get_task(UUID(run.domain_run_id))
    req = action(run, "confirm_outline", outline=domain.outline | {"title": "Edited SETEX"})
    queued = await deps.task_service.act(run.id, req)
    assert queued.status == "queued"
    assert (await deps.task_service.act(run.id, req)).id == run.id
    await deps.execution_worker.run_once()
    run = await deps.run_repository.get_run(run.id)
    assert run.status == "waiting" and run.pending_action.reason == "save_draft"
    saved = await deps.task_service.act(run.id, action(run, "save_draft", key="save"))
    assert saved.status == "queued"
    await deps.execution_worker.run_once()
    run = await deps.run_repository.get_run(run.id)
    assert run.status == "completed" and Path(run.result["saved_path"]).is_file()
    assert (await deps.task_service.act(run.id, req)).status == "completed"
    with pytest.raises(RunRepositoryConflict, match="action_request_conflict"):
        await deps.task_service.act(run.id, req.model_copy(update={"payload": {"outline": {}}}))
    assert llm.outline_calls == llm.draft_calls == 1


async def test_writing_empty_evidence_fails_without_outline(tmp_path, rag_database_path):
    deps, session, service, llm = await writing_setup(tmp_path, rag_database_path, empty=True)
    run = await deps.task_service.submit(session, request("writing"))
    await deps.execution_worker.run_once()
    run = await deps.run_repository.get_run(run.id)
    assert run.status == "failed" and run.pending_action is None
    assert llm.outline_calls == 0


async def crash_recovery(deps, run, mutate=None):
    repo = deps.execution_repository
    class Clock:
        value = datetime.now(timezone.utc)
        def now(self):
            return self.value
    clock = Clock()
    repo.clock = clock
    lease = await repo.claim(run.id, "dead", lease_seconds=1)
    if mutate:
        await mutate(lease)
    clock.value += timedelta(seconds=2)
    await deps.execution_worker.recover_on_startup()
    return await deps.run_repository.get_run(run.id)


async def test_domain_outline_publication_outer_crash_reconciles_no_model_replay(tmp_path, rag_database_path):
    deps, session, service, llm = await writing_setup(tmp_path, rag_database_path)
    run = await deps.task_service.submit(session, request("writing").model_copy(update={"message": "SETEX expiration"}))
    adapter = deps.task_service.adapters["writing"]
    async def publish(lease):
        domain = await service.create_task(session, "SETEX expiration")
        snapshot = await deps.execution_repository.get_snapshot(run.id)
        await deps.execution_repository.link_stage(lease, snapshot.model_copy(update={
            "domain_run_id": str(domain.task_id), "phase": "generating", "input_version": domain.version}))
        await adapter.executor.execute_research(domain.task_id, expected_version=domain.version)
    current = await crash_recovery(deps, run, publish)
    assert current.status == "waiting" and current.pending_action.reason == "confirm_outline"
    assert llm.outline_calls == 1 and not await deps.execution_worker.run_once()


async def test_unknown_save_outcome_keeps_waiting_and_rejects_regeneration(tmp_path, rag_database_path, monkeypatch):
    deps, session, service, llm = await writing_setup(tmp_path, rag_database_path)
    run = await deps.task_service.submit(session, request("writing").model_copy(update={"message": "SETEX expiration"}))
    await deps.execution_worker.run_once()
    run = await deps.run_repository.get_run(run.id)
    await deps.task_service.act(run.id, action(run, "confirm_outline", outline=run.result["outline"]))
    await deps.execution_worker.run_once()
    run = await deps.run_repository.get_run(run.id)
    async def unknown(*args, **kwargs):
        raise OSError("storage outcome unknown")
    monkeypatch.setattr(service, "save", unknown)
    await deps.task_service.act(run.id, action(run, "save_draft"))
    await deps.execution_worker.run_once()
    run = await deps.run_repository.get_run(run.id)
    assert run.status == "waiting" and run.pending_action.reason == "save_outcome_unknown"
    assert "confirm_outline" not in run.pending_action.allowed_actions
    with pytest.raises(RunRepositoryConflict):
        await deps.task_service.act(run.id, action(run, "confirm_outline", outline={}))
    assert llm.outline_calls == llm.draft_calls == 1


async def test_real_chat_adapter_links_only_one_domain_turn(tmp_path):
    from app.agent.models import LLMResponse
    deps, session = await setup(tmp_path)
    adapter = deps.task_service.adapters["chat"]
    class Model:
        async def complete(self, messages, tools):
            return LLMResponse(kind="final", content="hello back")
    adapter.runtime.loop.llm = Model()
    run = await deps.task_service.submit(session, request())
    await deps.execution_worker.run_once()
    run = await deps.run_repository.get_run(run.id)
    assert run.status == "completed" and run.domain_run_id
    events = await deps.session_repository.list_session_events(session)
    assert sum(e.event_type == "assistant_message" for e in events) == 1
    assert not any(e.event_type == "assistant_message" for e in await deps.run_repository.list_events(run.id))


async def test_recovered_outline_waiting_can_confirm_after_restart(tmp_path, rag_database_path):
    deps, session, service, llm = await writing_setup(tmp_path, rag_database_path)
    run = await deps.task_service.submit(session, request("writing").model_copy(update={"message": "SETEX expiration"}))
    adapter = deps.task_service.adapters["writing"]
    async def publish(lease):
        task = await service.create_task(session, "SETEX expiration")
        snapshot = await deps.execution_repository.get_snapshot(run.id)
        await deps.execution_repository.link_stage(lease, snapshot.model_copy(update={
            "domain_run_id": str(task.task_id), "phase": "generating"}))
        await adapter.executor.execute_research(task.task_id, expected_version=task.version)
    run = await crash_recovery(deps, run, publish)
    await deps.task_service.act(run.id, action(run, "confirm_outline", outline=run.result["outline"]))
    await deps.execution_worker.run_once()
    current = await deps.run_repository.get_run(run.id)
    assert current.status == "waiting" and current.pending_action.reason == "save_draft"
    assert llm.outline_calls == llm.draft_calls == 1


async def test_saved_domain_outer_crash_reconciles_without_second_save(tmp_path, rag_database_path, monkeypatch):
    deps, session, service, llm = await writing_setup(tmp_path, rag_database_path)
    run = await deps.task_service.submit(session, request("writing").model_copy(update={"message": "SETEX expiration"}))
    await deps.execution_worker.run_once()
    run = await deps.run_repository.get_run(run.id)
    await deps.task_service.act(run.id, action(run, "confirm_outline", outline=run.result["outline"]))
    await deps.execution_worker.run_once()
    run = await deps.run_repository.get_run(run.id)
    await deps.task_service.act(run.id, action(run, "save_draft"))
    run = await deps.run_repository.get_run(run.id)
    calls = []
    original = service.save
    async def save(*args, **kwargs):
        calls.append(True)
        return await original(*args, **kwargs)
    monkeypatch.setattr(service, "save", save)
    async def publish(lease):
        snapshot = await deps.execution_repository.get_snapshot(run.id)
        await deps.execution_repository.link_stage(lease, snapshot.model_copy(update={"phase": "generating"}))
        await service.save(UUID(run.domain_run_id), expected_version=snapshot.pending_action.business_version,
                           idempotency_key=snapshot.save_key)
    current = await crash_recovery(deps, run, publish)
    assert current.status == "completed" and len(calls) == 1
    assert not await deps.execution_worker.run_once()


async def test_chat_keeps_previous_completed_history(tmp_path):
    from app.agent.models import LLMResponse
    deps, session = await setup(tmp_path)
    adapter = deps.task_service.adapters["chat"]
    contexts = []
    class Model:
        async def complete(self, messages, tools):
            contexts.append(messages)
            return LLMResponse(kind="final", content="hello back")
    adapter.runtime.loop.llm = Model()
    await deps.task_service.submit(session, request().model_copy(update={"message": "first question"}))
    await deps.execution_worker.run_once()
    await deps.task_service.submit(session, request().model_copy(update={"message": "second question"}))
    await deps.execution_worker.run_once()
    assert any(m.content == "first question" for m in contexts[-1])


async def test_real_knowledge_adapter_and_publication_recovery(tmp_path):
    from app.knowledge.service import KnowledgeAnswer
    deps, session = await setup(tmp_path)
    adapter = deps.task_service.adapters["knowledge"]
    calls = []
    class Knowledge:
        async def answer(self, message, **kwargs):
            calls.append(message)
            return KnowledgeAnswer(answer="材料不足", retrieval_mode="keyword", evidence_status="insufficient", citations=[])
    adapter.conversation.service = Knowledge()
    run = await deps.task_service.submit(session, request("knowledge"))
    async def publish(lease):
        domain_id = await deps.session_repository.create_run(session)
        snapshot = await deps.execution_repository.get_snapshot(run.id)
        await deps.execution_repository.link_stage(lease, snapshot.model_copy(update={
            "domain_run_id": str(domain_id), "phase": "generating"}))
        await adapter.conversation.run(session, "hello", run_id=domain_id)
    current = await crash_recovery(deps, run, publish)
    assert current.status == "completed" and current.result["answer"] == "材料不足"
    assert calls == ["hello"]


async def test_chat_published_answer_outer_crash_preserves_answer(tmp_path):
    from app.agent.models import LLMResponse
    deps, session = await setup(tmp_path)
    adapter = deps.task_service.adapters["chat"]
    class Model:
        async def complete(self, messages, tools):
            return LLMResponse(kind="final", content="published answer")
    adapter.runtime.loop.llm = Model()
    run = await deps.task_service.submit(session, request())
    async def publish(lease):
        domain_id = await deps.session_repository.create_run(session)
        snapshot = await deps.execution_repository.get_snapshot(run.id)
        await deps.execution_repository.link_stage(lease, snapshot.model_copy(update={
            "domain_run_id": str(domain_id), "phase": "generating"}))
        await adapter.runtime.run(session, "hello", domain_id)
    current = await crash_recovery(deps, run, publish)
    assert current.status == "completed" and current.result["answer"] == "published answer"


async def test_unknown_save_inspect_is_read_only(tmp_path, rag_database_path, monkeypatch):
    deps, session, service, llm = await writing_setup(tmp_path, rag_database_path)
    run = await deps.task_service.submit(session, request("writing").model_copy(update={"message": "SETEX expiration"}))
    await deps.execution_worker.run_once()
    run = await deps.run_repository.get_run(run.id)
    await deps.task_service.act(run.id, action(run, "confirm_outline", outline=run.result["outline"]))
    await deps.execution_worker.run_once()
    run = await deps.run_repository.get_run(run.id)
    async def unknown(*args, **kwargs):
        raise OSError("unknown")
    monkeypatch.setattr(service, "save", unknown)
    await deps.task_service.act(run.id, action(run, "save_draft"))
    await deps.execution_worker.run_once()
    run = await deps.run_repository.get_run(run.id)
    inspected = await deps.task_service.act(run.id, action(run, "inspect_recovery"))
    assert inspected == run
    assert not await deps.execution_worker.run_once()


async def test_saved_domain_with_different_key_remains_unknown(tmp_path, rag_database_path):
    deps, session, service, llm = await writing_setup(tmp_path, rag_database_path)
    run = await deps.task_service.submit(session, request("writing").model_copy(update={"message": "SETEX expiration"}))
    await deps.execution_worker.run_once()
    run = await deps.run_repository.get_run(run.id)
    await deps.task_service.act(run.id, action(run, "confirm_outline", outline=run.result["outline"]))
    await deps.execution_worker.run_once()
    run = await deps.run_repository.get_run(run.id)
    await deps.task_service.act(run.id, action(run, "save_draft"))
    run = await deps.run_repository.get_run(run.id)
    async def publish(lease):
        snapshot = await deps.execution_repository.get_snapshot(run.id)
        await deps.execution_repository.link_stage(lease, snapshot.model_copy(update={"phase": "generating"}))
        await service.save(UUID(run.domain_run_id), expected_version=snapshot.pending_action.business_version,
                           idempotency_key="different-save-key")
    current = await crash_recovery(deps, run, publish)
    assert current.status == "waiting" and current.pending_action.reason == "save_outcome_unknown"