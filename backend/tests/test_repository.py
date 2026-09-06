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
        await repo.append_event(session_id, run_id, "user_message", {"message": "hello"})
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
