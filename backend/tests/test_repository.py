import json
from pathlib import Path
from uuid import uuid4

import aiosqlite
import pytest

from app.sessions.repository import SessionRepository


@pytest.mark.asyncio
async def test_sessions_are_independent(tmp_path: Path):
    repo = SessionRepository(tmp_path / "agent.db")
    await repo.init()
    first, second = await repo.create_session(), await repo.create_session()
    assert (
        first != second
        and await repo.session_exists(first)
        and await repo.session_exists(second)
    )


@pytest.mark.asyncio
async def test_create_run_rejects_unknown_session(tmp_path: Path):
    repo = SessionRepository(tmp_path / "agent.db")
    await repo.init()

    with pytest.raises(ValueError, match="Session not found"):
        await repo.create_run(uuid4())


@pytest.mark.asyncio
async def test_events_and_checkpoints_require_existing_run(tmp_path: Path):
    repo = SessionRepository(tmp_path / "agent.db")
    await repo.init()
    session_id = await repo.create_session()
    run_id = uuid4()

    with pytest.raises(ValueError, match="Run not found"):
        await repo.append_event(
            session_id, run_id, "user_message", {"message": "hello"}
        )
    with pytest.raises(ValueError, match="Run not found"):
        await repo.save_checkpoint(run_id, 1, {"step": "saved"})


@pytest.mark.asyncio
async def test_event_requires_run_to_belong_to_session(tmp_path: Path):
    repo = SessionRepository(tmp_path / "agent.db")
    await repo.init()
    session_id = await repo.create_session()
    another_session_id = await repo.create_session()
    run_id = await repo.create_run(session_id)

    with pytest.raises(ValueError, match="does not belong"):
        await repo.append_event(
            another_session_id, run_id, "user_message", {"message": "hello"}
        )


@pytest.mark.asyncio
async def test_events_and_checkpoints_are_written_for_valid_run(tmp_path: Path):
    repo = SessionRepository(tmp_path / "agent.db")
    await repo.init()
    session_id = await repo.create_session()
    run_id = await repo.create_run(session_id)

    sequence = await repo.append_event(
        session_id, run_id, "user_message", {"message": "hello"}
    )
    await repo.save_checkpoint(run_id, sequence, {"step": "saved"})

    async with aiosqlite.connect(repo.path) as db:
        event_cursor = await db.execute(
            "SELECT session_id, run_id, sequence, event_type, payload "
            "FROM events WHERE run_id = ?",
            (str(run_id),),
        )
        event = await event_cursor.fetchone()
        checkpoint_cursor = await db.execute(
            "SELECT run_id, sequence, state FROM checkpoints WHERE run_id = ?",
            (str(run_id),),
        )
        checkpoint = await checkpoint_cursor.fetchone()

    assert event == (
        str(session_id),
        str(run_id),
        1,
        "user_message",
        json.dumps({"message": "hello"}),
    )
    assert checkpoint == (str(run_id), 1, json.dumps({"step": "saved"}))


@pytest.mark.asyncio
async def test_repository_reads_runtime_state_for_recovery(tmp_path: Path):
    repo = SessionRepository(tmp_path / "agent.db")
    await repo.init()
    session_id = await repo.create_session({"memory": ["fact"]})
    run_id = await repo.create_run(session_id)

    await repo.update_run(run_id, "paused", {"phase": "waiting_tool"}, loop_count=2)
    first_sequence = await repo.append_event(
        session_id, run_id, "tool_call_started", {"call_id": "call-1"}
    )
    second_sequence = await repo.append_event(
        session_id, run_id, "tool_result", {"call_id": "call-1", "success": True}
    )
    await repo.save_checkpoint(run_id, second_sequence, {"phase": "waiting_llm"})

    session = await repo.get_session(session_id)
    run = await repo.get_run(run_id, session_id)
    events = await repo.list_events(run_id)
    remaining_events = await repo.list_events(run_id, after_sequence=first_sequence)
    checkpoint = await repo.get_checkpoint(run_id)

    assert session is not None and session.metadata == {"memory": ["fact"]}
    assert run is not None and run.status == "paused"
    assert run.loop_count == 2 and run.state == {"phase": "waiting_tool"}
    assert [event.sequence for event in events] == [1, 2]
    assert (
        len(remaining_events) == 1 and remaining_events[0].event_type == "tool_result"
    )
    assert checkpoint is not None
    assert checkpoint.sequence == second_sequence
    assert checkpoint.state == {"phase": "waiting_llm"}


@pytest.mark.asyncio
async def test_repository_reads_events_across_runs_for_one_session(tmp_path: Path):
    repo = SessionRepository(tmp_path / "agent.db")
    await repo.init()
    session_id = await repo.create_session()
    first_run_id = await repo.create_run(session_id)
    second_run_id = await repo.create_run(session_id)

    await repo.append_event(
        session_id, first_run_id, "user_message", {"message": "one"}
    )
    await repo.append_event(
        session_id, second_run_id, "user_message", {"message": "two"}
    )

    events = await repo.list_session_events(session_id)

    assert [event.run_id for event in events] == [first_run_id, second_run_id]
    assert [event.payload["message"] for event in events] == ["one", "two"]


@pytest.mark.asyncio
async def test_repository_rejects_invalid_run_state_values(tmp_path: Path):
    repo = SessionRepository(tmp_path / "agent.db")
    await repo.init()
    session_id = await repo.create_session()
    run_id = await repo.create_run(session_id)

    with pytest.raises(ValueError, match="loop_count cannot be negative"):
        await repo.update_run(run_id, "running", loop_count=-1)
    with pytest.raises(ValueError, match="sequence cannot be negative"):
        await repo.save_checkpoint(run_id, -1, {})
    with pytest.raises(ValueError, match="Run not found"):
        await repo.update_run(uuid4(), "failed")


@pytest.mark.asyncio
async def test_list_session_events_rejects_unknown_session(tmp_path: Path):
    repo = SessionRepository(tmp_path / "agent.db")
    await repo.init()

    with pytest.raises(ValueError, match="Session not found"):
        await repo.list_session_events(uuid4())


@pytest.mark.asyncio
async def test_get_run_rejects_wrong_session(tmp_path: Path):
    repo = SessionRepository(tmp_path / "agent.db")
    await repo.init()
    session_id = await repo.create_session()
    another_session_id = await repo.create_session()
    run_id = await repo.create_run(session_id)

    with pytest.raises(ValueError, match="does not belong"):
        await repo.get_run(run_id, another_session_id)
