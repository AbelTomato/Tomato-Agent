from uuid import UUID

import pytest
from pydantic import ValidationError

from app.runs.models import PendingAction
from app.tasks.models import TaskActionRequest, UnifiedTaskRequest
from test_unified_tasks_api import api, body


def action(name, payload=None, **changes):
    return TaskActionRequest(action_id="pending-1", action=name, expected_version=2,
        client_request_id="action-1", payload=payload or {}, **changes)


def outline():
    return {"title": "缓存", "sections": [{"title": "失效", "points": ["过期"],
        "citation_ids": ["evidence-1"]}], "gaps": []}


@pytest.mark.parametrize("decision", [
    {"type": "message", "payload": {"text": "阶段总结"}},
    {"type": "ask_user", "payload": {"question": "选择哪个？", "choices": ["A", "B"], "allow_free_text": True}},
    {"type": "discover_tools", "payload": {"category": "project", "query": "读取"}},
    {"type": "describe_tool", "payload": {"name": "project.read_file"}},
    {"type": "call_tool", "payload": {"name": "project.read_file", "contract_version": "1", "arguments": {"path": "README.md"}}},
    {"type": "finish", "payload": {"result": {"answer": "完成"}}},
])
def test_assistant_decisions_are_versioned_and_discriminated(decision):
    from pydantic import TypeAdapter
    from app.assistant.models import AssistantDecision

    parsed = TypeAdapter(AssistantDecision).validate_python({"protocol_version": 1, **decision})
    assert parsed.type == decision["type"]
    assert parsed.protocol_version == 1


@pytest.mark.parametrize("decision", [
    {"protocol_version": 2, "type": "message", "payload": {"text": "x"}},
    {"protocol_version": 1, "type": "message", "payload": {"text": "x", "run_id": "forged"}},
    {"protocol_version": 1, "type": "message", "payload": {"text": "x"}, "run_id": "forged"},
    {"protocol_version": 1, "type": "finish", "payload": {}},
    {"protocol_version": 1, "type": "call_tool", "payload": {"name": "tool", "contract_version": "1"}},
    {"protocol_version": 1, "type": "unknown", "payload": {}},
])
def test_assistant_decisions_reject_unknown_versions_extra_fields_and_invalid_payloads(decision):
    from pydantic import TypeAdapter
    from app.assistant.models import AssistantDecision

    with pytest.raises(ValidationError):
        TypeAdapter(AssistantDecision).validate_python(decision)


def test_runtime_event_is_separate_versioned_and_strict():
    from datetime import datetime, timezone
    from uuid import uuid4

    from app.assistant.models import RuntimeEvent

    event = RuntimeEvent(event_id=uuid4(), run_id=uuid4(), step_id="step-1", invocation_id="call-1",
        event_type="tool.started", occurred_at=datetime.now(timezone.utc), summary="工具已启动",
        detail={"data": {"name": "project.read_file"}})
    assert RuntimeEvent.model_validate_json(event.model_dump_json()) == event
    with pytest.raises(ValidationError):
        RuntimeEvent(**{**event.model_dump(), "schema_version": 2})
    with pytest.raises(ValidationError):
        RuntimeEvent(**{**event.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError):
        RuntimeEvent(**event.model_dump(), type="message")


def test_runtime_event_detail_rejects_untyped_values():
    from app.assistant.models import RuntimeEventDetail

    with pytest.raises(ValidationError):
        RuntimeEventDetail(data={"run_id": object()})


@pytest.mark.parametrize("workspace", [None, "registered-project"])
def test_assistant_workspace_is_optional(workspace):
    request = UnifiedTaskRequest(kind="assistant", message="解释缓存失效",
        client_request_id="request-1", workspace_id=workspace, options={})
    assert request.kind == "assistant" and request.workspace_id == workspace


@pytest.mark.parametrize("changes", [
    {"message": ""}, {"message": " \n "}, {"options": {"limit": 5}},
    {"options": {"allow_process": True}}, {"workspace_id": " "},
    {"client_request_id": " "}, {"authorized": True},
])
def test_assistant_rejects_invalid_request(changes):
    with pytest.raises(ValidationError):
        UnifiedTaskRequest(**{**body(kind="assistant"), **changes})


@pytest.mark.parametrize("kind,workspace", [("code", None), ("chat", "project"),
    ("knowledge", "project"), ("writing", "project")])
def test_legacy_workspace_rules_remain(kind, workspace):
    with pytest.raises(ValidationError):
        UnifiedTaskRequest(**body(kind=kind, workspace_id=workspace))


@pytest.mark.parametrize("name,payload", [
    ("provide_input", {"message": "使用当前项目"}),
    ("bind_workspace", {"workspace_id": "registered-project"}),
    ("approve_action", {}), ("approve_action", {"outline": outline()}),
    ("reject_action", {}),
])
def test_public_action_payloads(name, payload):
    request = action(name, payload)
    assert request.payload == payload
    assert TaskActionRequest.model_validate_json(request.model_dump_json()) == request


@pytest.mark.parametrize("name,payload", [
    ("provide_input", {}), ("provide_input", {"message": " "}),
    ("provide_input", {"message": 1}), ("provide_input", {"message": "ok", "authorized": True}),
    ("bind_workspace", {}), ("bind_workspace", {"workspace_id": " "}),
    ("bind_workspace", {"workspace_id": 1}),
    ("bind_workspace", {"workspace_id": "project", "path": "/etc"}),
    ("bind_workspace", {"workspace_id": "project", "authorized": True}),
    ("approve_action", {"tool_name": "shell", "command": "anything"}),
    ("approve_action", {"outline": {}}),
    ("approve_action", {"outline": outline(), "approved": True}),
    ("reject_action", {"confirmed": True}),
])
def test_public_action_payloads_are_strict(name, payload):
    with pytest.raises(ValidationError):
        action(name, payload)


@pytest.mark.parametrize("field,value", [("expected_version", -1), ("expected_version", True),
    ("expected_version", "2"), ("action_id", " "), ("client_request_id", " ")])
def test_action_identity_and_version_are_strict(field, value):
    data = {"action_id": "pending-1", "action": "bind_workspace", "expected_version": 2,
        "client_request_id": "action-1", "payload": {"workspace_id": "project"}}
    with pytest.raises(ValidationError):
        TaskActionRequest(**{**data, field: value})


def test_pending_action_retains_server_frozen_metadata_and_legacy_json():
    old_json = '{"action_id":"old","reason":"outline","business_version":1,"allowed_actions":["confirm_outline","cancel"],"display_data":{}}'
    legacy = PendingAction(action_id="old", reason="outline", business_version=1,
        allowed_actions=["confirm_outline", "cancel"])
    assert PendingAction.model_validate_json(old_json) == legacy
    assert PendingAction.model_validate_json(legacy.model_dump_json()) == legacy
    pending = PendingAction(action_id="bind-1", reason="workspace_required", business_version=2,
        allowed_actions=["bind_workspace", "reject_action", "cancel"], action_type="workspace_binding",
        target_capability="project.read_file", frozen_parameters={"purpose": "read"})
    assert pending.target_capability == "project.read_file"
    assert pending.frozen_parameters == {"purpose": "read"}
    assert PendingAction.model_validate_json(pending.model_dump_json()) == pending


@pytest.mark.parametrize("status", ["not_required", "passed", "failed", "unavailable"])
def test_result_validation_status_is_independent_of_run_status(status):
    from app.assistant.models import AssistantResult
    result = AssistantResult(answer="解释", validation_status=status)
    assert result.validation_status == status
    assert result.citations == result.artifacts == result.test_results == []
    assert AssistantResult.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        AssistantResult(validation_status="completed")


@pytest.mark.parametrize("category", ["test_failed", "recoverable_error", "permission_denied",
    "user_rejected", "unavailable", "ownership_lost", "unknown_side_effect", "internal_error"])
def test_capability_failure_is_structured(category):
    from app.assistant.models import CapabilityOutcome
    outcome = CapabilityOutcome(kind="failed", error={"category": category, "code": "safe_reason"})
    assert outcome.error.category == category
    assert CapabilityOutcome.model_validate_json(outcome.model_dump_json()) == outcome


def test_capability_outcomes_require_matching_result_action_or_error():
    from app.assistant.models import CapabilityOutcome
    pending = PendingAction(action_id="input", reason="clarify", business_version=0,
        allowed_actions=["provide_input"], action_type="clarification")
    assert CapabilityOutcome(kind="waiting", pending_action=pending).pending_action == pending
    assert CapabilityOutcome(kind="result", result={"answer": "ok"}).result.answer == "ok"
    assert CapabilityOutcome(kind="waiting", pending_action=pending,
        error={"category": "unknown_side_effect", "code": "observe_required"}).error.category == "unknown_side_effect"
    for data in [{"kind": "waiting"}, {"kind": "failed"},
        {"kind": "result", "error": {"category": "internal_error", "code": "error"}},
        {"kind": "waiting", "pending_action": pending, "error": {"category": "internal_error", "code": "error"}}]:
        with pytest.raises(ValidationError):
            CapabilityOutcome(**data)


def test_result_reuses_existing_citation_and_artifact_contracts():
    from datetime import datetime, timezone
    from uuid import uuid4

    from app.assistant.models import AssistantResult

    result = AssistantResult(citations=[{"citation_id": "evidence-1", "chunk_id": "chunk",
        "document_id": "document", "document_version": "v1", "source_path": "guide.md",
        "title": "Guide", "heading_path": "Cache", "start_line": 1, "end_line": 2,
        "text": "Evidence"}], artifacts=[{"artifact_id": uuid4(), "run_id": uuid4(),
        "kind": "diff", "size_bytes": 1, "sha256": "a" * 64,
        "created_at": datetime.now(timezone.utc)}],
        test_results=[{"passed": False, "exit_code": 1}], validation_status="failed")
    assert result.citations[0].citation_id == "evidence-1"
    assert result.artifacts[0].kind == "diff"
    assert result.test_results[0]["passed"] is False
    assert AssistantResult.model_validate_json(result.model_dump_json()) == result


def test_capability_descriptor_exposes_server_recovery_and_availability_metadata():
    from app.assistant.models import CapabilityDescriptor
    descriptor = CapabilityDescriptor(name="project.read_file", parameters={"type": "object"},
        requires_workspace=True, side_effect="read_only", recovery_class="read_only",
        available=True, completion_requirements=[])
    assert descriptor.requires_workspace is True
    assert descriptor.available is True
    with pytest.raises(ValidationError):
        CapabilityDescriptor(**{**descriptor.model_dump(), "authorized": True})


async def test_assistant_api_is_disabled_without_creating_run(tmp_path):
    async with api(tmp_path) as (app, client, session):
        response = await client.post(f"/api/sessions/{session}/tasks", json=body(kind="assistant"))
        assert response.status_code == 503
        assert response.json()["detail"] == {"code": "assistant_disabled"}
        assert await app.state.run_repository.list_for_session(UUID(session)) == []


async def test_new_actions_do_not_grant_legacy_task_permissions(tmp_path):
    async with api(tmp_path) as (app, client, session):
        run = (await client.post(f"/api/sessions/{session}/tasks", json=body())).json()
        request = action("bind_workspace", {"workspace_id": "project"}).model_dump()
        request["expected_version"] = run["version"]
        response = await client.post(f"/api/tasks/{run['run_id']}/actions", json=request)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "action_not_allowed"
        assert (await app.state.run_repository.get_run(UUID(run["run_id"]))).workspace_id is None


async def test_public_action_replay_checks_entire_request_and_version(tmp_path):
    import aiosqlite
    from app.execution.ports import canonical_digest
    from app.runs.models import RunRepositoryConflict

    async with api(tmp_path) as (app, client, session):
        run = (await client.post(f"/api/sessions/{session}/tasks", json=body())).json()
        request = action("bind_workspace", {"workspace_id": "project"})
        repo = app.state.execution_repository
        # Exercise the existing shared action ledger without enabling assistant execution.
        async with aiosqlite.connect(repo.path) as db:
            await db.execute("INSERT INTO task_actions(run_id,client_request_id,digest) VALUES (?,?,?)",
                (run["run_id"], request.client_request_id, canonical_digest(request.model_dump(mode="json"))))
            await db.commit()
        assert (await repo.get_stage_action_replay(UUID(run["run_id"]), request)).id == UUID(run["run_id"])
        for changes in [{"payload": {"workspace_id": "other"}}, {"expected_version": 3},
            {"action_id": "other"}, {"action": "reject_action", "payload": {}}]:
            changed = TaskActionRequest.model_validate({**request.model_dump(), **changes})
            with pytest.raises(RunRepositoryConflict, match="action_request_conflict"):
                await repo.get_stage_action_replay(UUID(run["run_id"]), changed)