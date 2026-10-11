import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4

import aiosqlite
import pytest
from pydantic import ValidationError

from app.execution.repository import ExecutionRepository
from app.runs.models import RunRepositoryConflict
from app.runs.repository import RunRepository
from app.sessions.repository import SessionRepository


async def repositories(path):
    sessions = SessionRepository(path)
    await sessions.init()
    runs = RunRepository(path)
    await runs.init()
    return sessions, runs


@pytest.mark.parametrize("kind", ["chat", "knowledge", "writing", "code"])
async def test_session_run_kind_workspace_and_history(tmp_path, kind):
    sessions, runs = await repositories(tmp_path / "runs.db")
    session_id = await sessions.create_session()
    workspace = "workspace" if kind == "code" else None
    run = await runs.create_session_run(session_id, kind, {"message": "hello"}, workspace, "request-1")
    assert run.session_id == session_id
    assert run.task_type == kind
    assert run.workspace_id == workspace
    assert run.client_request_id == "request-1"
    assert run.result is None and run.pending_action is None and run.stage is None
    assert await runs.get_run(run.id) == run
    assert await runs.list_for_session(session_id, 10, 0) == [run]
    assert await runs.list_for_session(uuid4(), 10, 0) == []
    assert await runs.list_for_session(session_id, 10, 1) == []
    assert await ExecutionRepository(runs.path).get_run(run.id) == run


async def test_request_replay_conflict_and_session_exclusion(tmp_path):
    sessions, runs = await repositories(tmp_path / "runs.db")
    session_id = await sessions.create_session()
    args = (session_id, "chat", {"message": "hello", "options": {"a": 1, "b": 2}}, None, "one")
    first, replay = await asyncio.gather(runs.create_session_run(*args), runs.create_session_run(*args))
    assert first == replay
    assert await runs.create_session_run(session_id, "chat", {"options": {"b": 2, "a": 1}, "message": "hello"}, None, "one") == first
    for kind, request, workspace in [("chat", {"message": "changed"}, None), ("writing", args[2], None), ("chat", args[2], "other")]:
        with pytest.raises(RunRepositoryConflict, match="request_conflict"):
            await runs.create_session_run(session_id, kind, request, workspace, "one")
    with pytest.raises(RunRepositoryConflict, match="session_busy"):
        await runs.create_session_run(session_id, "chat", {}, None, "two")
    await runs.update_status(first.id, "running")
    await runs.update_status(first.id, "waiting")
    with pytest.raises(RunRepositoryConflict, match="session_busy"):
        await runs.create_session_run(session_id, "chat", {}, None, "two")
    await runs.update_status(first.id, "completed")
    second = await runs.create_session_run(session_id, "chat", {}, None, "two")
    assert await runs.list_for_session(session_id, 1, 1) == [second]
    other = await sessions.create_session()
    assert (await runs.create_session_run(other, "chat", {}, None, "one")).id != first.id
    with pytest.raises(KeyError):
        await runs.create_session_run(uuid4(), "chat", {}, None, "one")
    with pytest.raises(ValueError):
        await runs.create_session_run(other, "code_task", {}, None, "invalid")
    with pytest.raises(ValueError):
        await runs.create_session_run(other, "code", {}, None, "invalid")
    with pytest.raises(ValueError):
        await runs.list_for_session(session_id, 10, -1)


def test_public_state_machine():
    from app.runs.state_machine import transition_run

    allowed = {
        "queued": {"running", "cancelled", "timed_out", "failed"},
        "running": {"waiting", "completed", "failed", "cancelled", "timed_out"},
        "waiting": {"running", "completed", "failed", "cancelled", "timed_out"},
        "completed": set(), "failed": set(), "cancelled": set(), "timed_out": set(),
    }
    for current in allowed:
        for target in allowed:
            if target == current or target in allowed[current]:
                assert transition_run(current, target) == target
            else:
                with pytest.raises(ValueError):
                    transition_run(current, target)
    assert transition_run("waiting", "queued", resume_confirmed=True) == "queued"
    with pytest.raises(ValueError):
        transition_run("completed", "queued", resume_confirmed=True)


async def test_resume_requires_confirmation_and_respects_execution_owner(tmp_path):
    runs = RunRepository(tmp_path / "runs.db")
    await runs.init()
    run = await runs.create_run("code_task", {}, "workspace")
    await runs.update_status(run.id, "running")
    await runs.update_status(run.id, "waiting")
    with pytest.raises(ValueError):
        await runs.update_status(run.id, "queued")
    assert (await runs.update_status(run.id, "queued", resume_confirmed=True)).status == "queued"
    execution = ExecutionRepository(runs.path)
    await execution.init()
    lease = await execution.claim(run.id, "worker", lease_seconds=30)
    with pytest.raises(RuntimeError, match="execution owner"):
        await runs.update_status(run.id, "queued", resume_confirmed=True)
    await execution.finish(lease, "waiting")
    with pytest.raises(ValueError):
        await execution.resume(run.id, expected_version=(await runs.get_run(run.id)).version, confirmed=False)
    version = (await runs.get_run(run.id)).version
    with pytest.raises(RunRepositoryConflict):
        await execution.resume(run.id, expected_version=version - 1, confirmed=True)
    resumed = await execution.resume(run.id, expected_version=version, confirmed=True)
    assert resumed.status == "queued"
    assert (await runs.list_events(run.id))[-1].event_type == "execution.resumed"


async def test_stage_snapshot_roundtrip_and_strict_discriminator(tmp_path):
    from app.execution.models import StageExecutionSnapshot, parse_execution_snapshot

    sessions, runs = await repositories(tmp_path / "runs.db")
    run = await runs.create_session_run(await sessions.create_session(), "writing", {}, None, "one")
    snapshot = StageExecutionSnapshot(
        run_id=run.id, kind="writing", checkpoint_revision=1, last_event_sequence=0,
        phase="waiting", stage="outline", domain_run_id="domain-1", input_version=2,
        policy_digest="policy", config_digest="config", pending_action={
            "action_id": "outline-1", "reason": "confirm_outline", "allowed_actions": ["confirm_outline"],
            "business_version": 2, "display_data": {"outline": []},
        },
    )
    assert snapshot.snapshot_type == "stage" and snapshot.workspace_id is None
    assert parse_execution_snapshot(snapshot.model_dump_json()) == snapshot
    execution = ExecutionRepository(runs.path)
    await execution.init()
    await execution.bootstrap_snapshot(snapshot)
    assert await execution.get_snapshot(run.id) == snapshot
    raw = snapshot.model_dump(mode="json")
    for key, value in [("snapshot_type", "unknown"), ("schema_version", 99), ("kind", "code"), ("test_results", [])]:
        with pytest.raises(ValueError):
            parse_execution_snapshot(json.dumps({**raw, key: value}))
    with pytest.raises(ValidationError):
        StageExecutionSnapshot.model_validate_json(json.dumps({**raw, "pending_action": {"reason": "confirm"}}))


async def test_result_stage_action_and_event_commit_atomically(tmp_path):
    from app.runs.models import PendingAction

    sessions, runs = await repositories(tmp_path / "runs.db")
    run = await runs.create_session_run(await sessions.create_session(), "writing", {}, None, "one")
    await runs.update_status(run.id, "running")
    action = PendingAction(action_id="outline", reason="confirm_outline", allowed_actions=["confirm_outline"], business_version=1)
    waiting = await runs.update_status(
        run.id, "waiting", expected_version=1, stage="outline", pending_action=action,
        domain_run_id="writing-1", event_type="run.waiting", event_payload={"schema_version": 1},
    )
    assert waiting.pending_action == action and waiting.stage == "outline"
    assert waiting.domain_run_id == "writing-1"
    with pytest.raises(RunRepositoryConflict):
        await runs.update_status(run.id, "completed", expected_version=1, result={"text": "stale"}, event_type="run.result")
    assert await runs.get_run(run.id) == waiting
    assert len(await runs.list_events(run.id)) == 1
    completed = await runs.update_status(
        run.id, "completed", expected_version=2, result={"text": "draft", "token": "masked"},
        pending_action=None, stage="saved", event_type="run.result", event_payload={"schema_version": 1},
    )
    assert completed.result == {"text": "draft", "token": "[REDACTED]"}
    assert completed.pending_action is None and completed.stage == "saved"
    assert (await runs.list_events(run.id))[-1].event_type == "run.result"


async def test_resume_cannot_bypass_unknown_side_effects_or_active_owner(tmp_path):
    from app.execution.recovery import RecoveryConflict

    runs = RunRepository(tmp_path / "runs.db")
    await runs.init()
    run = await runs.create_run("code_task", {}, "workspace")
    execution = ExecutionRepository(runs.path)
    await execution.init()
    lease = await execution.claim(run.id, "worker", lease_seconds=30)
    await execution.finish(lease, "waiting")
    async with aiosqlite.connect(runs.path) as db:
        await db.execute(
            "INSERT INTO execution_operations (id, run_id, logical_index, kind, input_digest, input_payload, recovery_class, status) "
            "VALUES (?, ?, 0, 'tool', 'digest', '{}', 'file_write', 'unknown')", (str(uuid4()), str(run.id)),
        )
        await db.commit()
    with pytest.raises(RecoveryConflict, match="manual_recovery_required"):
        await execution.resume(run.id, expected_version=(await runs.get_run(run.id)).version, confirmed=True)
    async with aiosqlite.connect(runs.path) as db:
        await db.execute("UPDATE execution_run_heads SET active_attempt_id=? WHERE run_id=?", (str(lease.attempt_id), str(run.id)))
        await db.commit()
    with pytest.raises(RecoveryConflict, match="active_execution_owner"):
        await execution.resume(run.id, expected_version=(await runs.get_run(run.id)).version, confirmed=True)


async def test_old_schema_migrates_twice_without_losing_references(tmp_path):
    from app.execution.models import ExecutionSnapshot, parse_execution_snapshot

    path = tmp_path / "legacy.db"
    run_id, event_id = uuid4(), uuid4()
    timestamp = datetime.now(timezone.utc).isoformat()
    snapshot = ExecutionSnapshot(
        run_id=run_id, schema_version=1, checkpoint_revision=1, last_event_sequence=1,
        phase="ready_model", logical_index=0, messages=[], harness_state={}, budget_limits={"max_model_calls": 1},
        reserved_model_calls=0, reserved_tool_calls=0, loop_count=0, elapsed_upper_bound_seconds=0.0,
        policy_digest="policy", workspace_id="workspace", test_results=[], artifact_ids=[],
    )
    raw = snapshot.model_dump(mode="json")
    raw.pop("snapshot_type", None)
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript("""
            CREATE TABLE task_runs (id TEXT PRIMARY KEY, task_type TEXT NOT NULL, request TEXT NOT NULL,
                workspace_id TEXT NOT NULL, status TEXT NOT NULL, error TEXT, version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE INDEX legacy_run_status ON task_runs(status);
            CREATE TABLE task_run_events (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES task_runs(id),
                sequence INTEGER NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(run_id, sequence));
            CREATE TABLE task_run_checkpoints (run_id TEXT PRIMARY KEY REFERENCES task_runs(id),
                sequence INTEGER NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        await db.execute("INSERT INTO task_runs VALUES (?, 'code_task', '{}', 'workspace', 'queued', NULL, 0, ?, ?)", (str(run_id), timestamp, timestamp))
        await db.execute("INSERT INTO task_run_events VALUES (?, ?, 1, 'created', '{}', ?)", (str(event_id), str(run_id), timestamp))
        await db.execute("INSERT INTO task_run_checkpoints VALUES (?, 1, ?, ?)", (str(run_id), json.dumps(raw), timestamp))
        await db.commit()
    execution = ExecutionRepository(path)
    await execution.init()
    # Seed a historical execution using the old schema, before running migration.
    attempt_id = uuid4()
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("INSERT INTO execution_run_heads (run_id, fencing_token, protocol_version) VALUES (?, 1, 1)", (str(run_id),))
        await db.execute(
            "INSERT INTO execution_attempts (id, run_id, attempt_no, owner_id, fencing_token, status, lease_expires_at, heartbeat_at, started_at) "
            "VALUES (?, ?, 1, 'legacy-worker', 1, 'released', ?, ?, ?)",
            (str(attempt_id), str(run_id), timestamp, timestamp, timestamp),
        )
        await db.commit()
    runs = RunRepository(path)
    await runs.init()
    await runs.init()
    assert (await runs.get_run(run_id)).session_id is None
    assert (await runs.get_run(run_id)).workspace_id == "workspace"
    assert (await runs.list_events(run_id))[0].id == event_id
    assert await execution.get_snapshot(run_id) == parse_execution_snapshot(json.dumps(raw))
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        assert await (await db.execute("PRAGMA foreign_keys")).fetchone() == (1,)
        assert await (await db.execute("PRAGMA foreign_key_check")).fetchall() == []
        assert await (await db.execute("SELECT count(*) FROM task_runs")).fetchone() == (1,)
        assert await (await db.execute("SELECT count(*) FROM execution_attempts")).fetchone() == (1,)
        indexes = await (await db.execute("PRAGMA index_list(task_runs)")).fetchall()
        assert "legacy_run_status" in {row[1] for row in indexes}
        columns = await (await db.execute("PRAGMA table_info(task_runs)")).fetchall()
        assert next(row for row in columns if row[1] == "workspace_id")[3] == 0
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute("INSERT INTO task_run_checkpoints VALUES ('missing', 1, '{}', ?)", (timestamp,))
    legacy = await runs.create_run("code_task", {}, "another")
    assert legacy.session_id is None