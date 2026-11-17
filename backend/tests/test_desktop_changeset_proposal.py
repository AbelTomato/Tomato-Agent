import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.assistant.tools.project import ProposeChangeSet, ProposeChangeSetArguments
from app.execution.repository import validate_changeset_commit_receipt
from app.runs.models import PendingAction, RunRepositoryConflict
from app.tasks.models import TaskActionRequest
from app.tools.base import ToolContext


class FakeRuns:
    def __init__(self, session_id):
        self.session_id = session_id

    async def get_run(self, _run_id):
        return SimpleNamespace(session_id=self.session_id)


class FakeSessions:
    async def get_session(self, _session_id):
        return SimpleNamespace(project_id="project-1")


@pytest.mark.asyncio
async def test_changeset_proposal_waits_for_explicit_approval_and_binds_full_diff():
    session_id = str(uuid4())
    tool = ProposeChangeSet(FakeRuns(session_id), FakeSessions())
    context = ToolContext(
        session_id=session_id,
        run_id="run-1",
        project_id="project-1",
        project_read_grant="private-grant",
    )
    old = "before\n"
    result = await tool.execute({
        "changes": [{
            "relative_path": "src/app.py",
            "operation": "modify",
            "expected_old_sha256": hashlib.sha256(old.encode()).hexdigest(),
            "base_content": old,
            "content": "after\n",
            "summary": "Update output",
        }]
    }, context)

    assert result.success
    outcome = result.data
    assert outcome["kind"] == "waiting"
    action = outcome["pending_action"]
    proposed = action["display_data"]["changeset"]
    assert action["action_type"] == "changeset_approval"
    assert action["allowed_actions"] == ["approve_action", "reject_action"]
    assert proposed["run_id"] == "run-1"
    assert proposed["session_id"] == session_id
    assert proposed["project_id"] == "project-1"
    assert proposed["authorization_version"] != "private-grant"
    assert proposed["changes"][0]["base_content"] == old
    assert proposed["changes"][0]["content"] == "after\n"
    assert action["display_data"]["digest"]
    assert "private-grant" not in json.dumps(action)


@pytest.mark.asyncio
async def test_changeset_proposal_rejects_missing_run_grant_and_project_binding():
    session_id = str(uuid4())
    tool = ProposeChangeSet(FakeRuns(session_id), FakeSessions())
    context = ToolContext(session_id=session_id, run_id="run-1", project_id="project-1")

    with pytest.raises(PermissionError, match="project_read_authorization_required"):
        await tool.preflight({}, context)

    context.project_read_grant = "grant"
    context.project_id = "other-project"
    with pytest.raises(PermissionError, match="permission_denied"):
        await tool.preflight({}, context)


def test_changeset_tool_schema_rejects_unknown_proposal_fields():
    with pytest.raises(Exception):
        ProposeChangeSetArguments.model_validate({"changes": [], "absolute_root": "/tmp"})


def test_approve_action_requires_a_complete_structured_commit_receipt():
    base = {
        "action_id": "action-1",
        "action": "approve_action",
        "expected_version": 4,
        "client_request_id": "request-1",
        "payload": {"commit_receipt": {
            "schema_version": 1,
            "change_id": "change-1",
            "digest": "a" * 64,
            "status": "completed",
            "files": [{"relative_path": "src/app.py", "status": "applied", "error": None}],
        }},
    }
    assert TaskActionRequest.model_validate(base).action == "approve_action"
    incomplete = {**base, "payload": {"commit_receipt": {
        **base["payload"]["commit_receipt"], "status": "partial",
    }}}
    with pytest.raises(Exception, match="completed_changeset_receipt_required"):
        TaskActionRequest.model_validate(incomplete)


def test_repository_accepts_only_receipt_matching_frozen_digest_and_exact_path_set():
    pending = PendingAction(
        action_id="action-1", reason="changeset_approval_required", business_version=0,
        action_type="changeset_approval", target_capability="propose_changeset",
        frozen_parameters={"change_id": "change-1", "digest": "a" * 64},
        display_data={"changeset": {"changes": [
            {"relative_path": "src/app.py"}, {"relative_path": "README.md"},
        ]}},
        allowed_actions=["approve_action", "reject_action"],
    )
    receipt = {"change_id": "change-1", "digest": "a" * 64, "status": "completed",
        "files": [
            {"relative_path": "src/app.py", "status": "applied", "error": None},
            {"relative_path": "README.md", "status": "applied", "error": None},
        ]}
    validate_changeset_commit_receipt(pending, receipt)

    for changed in (
        {**receipt, "digest": "b" * 64},
        {**receipt, "files": list(reversed(receipt["files"]))},
        {**receipt, "files": [{**receipt["files"][0], "status": "failed"}, receipt["files"][1]]},
    ):
        with pytest.raises(RunRepositoryConflict, match="changeset_commit_unavailable"):
            validate_changeset_commit_receipt(pending, changed)
