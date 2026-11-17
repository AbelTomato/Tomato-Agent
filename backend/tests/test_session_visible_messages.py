from uuid import UUID

import pytest

from app.sessions.repository import SessionRepository


@pytest.mark.asyncio
async def test_visible_messages_have_stable_order_and_retry_by_message_id(tmp_path):
    repo = SessionRepository(tmp_path / "sessions.db")
    await repo.init()
    session_id = await repo.create_session()
    run_id = await repo.create_run(session_id)

    user = await repo.append_visible_message(
        session_id,
        message_id="legacy-user-1",
        role="user",
        content="你好",
        created_at="2026-11-01T00:00:00+00:00",
    )
    duplicate = await repo.append_visible_message(
        session_id,
        message_id="legacy-user-1",
        role="user",
        content="你好",
        created_at="2026-11-01T00:00:00+00:00",
    )
    assistant = await repo.append_visible_message(
        session_id,
        message_id="assistant-1",
        role="assistant",
        content="你好。",
        run_id=run_id,
        message_kind="final",
    )

    messages = await repo.list_visible_messages(session_id)
    assert user.id == duplicate.id == "legacy-user-1"
    assert assistant.sequence == user.sequence + 1
    assert [message.id for message in messages] == ["legacy-user-1", "assistant-1"]
    assert messages[1].run_id == run_id
    assert messages[1].message_kind == "final"


@pytest.mark.asyncio
async def test_visible_message_retry_rejects_changed_content_and_deduplicates_final_kind(tmp_path):
    repo = SessionRepository(tmp_path / "sessions.db")
    await repo.init()
    session_id = await repo.create_session()
    run_id = await repo.create_run(session_id)
    await repo.append_visible_message(
        session_id, message_id="user-1", role="user", content="question"
    )
    await repo.append_visible_message(
        session_id,
        message_id="assistant-1",
        role="assistant",
        content="answer",
        run_id=run_id,
        message_kind="final",
    )

    with pytest.raises(ValueError, match="visible_message_conflict"):
        await repo.append_visible_message(
            session_id, message_id="user-1", role="user", content="changed"
        )
    with pytest.raises(ValueError, match="visible_message_conflict"):
        await repo.append_visible_message(
            session_id,
            message_id="assistant-2",
            role="assistant",
            content="second final",
            run_id=run_id,
            message_kind="final",
        )


@pytest.mark.asyncio
async def test_visible_messages_reject_unknown_session_and_invalid_role(tmp_path):
    repo = SessionRepository(tmp_path / "sessions.db")
    await repo.init()
    with pytest.raises(ValueError, match="Session not found"):
        await repo.list_visible_messages(UUID(int=1))

    session_id = await repo.create_session()
    with pytest.raises(ValueError, match="invalid_visible_message"):
        await repo.append_visible_message(
            session_id, message_id="tool-1", role="tool", content="not user-visible"
        )


@pytest.mark.asyncio
async def test_desktop_history_import_is_atomic_idempotent_and_preserves_ids(tmp_path):
    repo = SessionRepository(tmp_path / "sessions.db")
    await repo.init()
    source = {
        "installation_id": "desktop-installation-1",
        "sessions": [{
            "id": "00000000-0000-0000-0000-000000000201",
            "title": "历史会话",
            "project_id": "project-1",
            "created_at": "2026-11-01T00:00:00+00:00",
        }],
        "messages": [{
            "id": "message-1",
            "session_id": "00000000-0000-0000-0000-000000000201",
            "sequence": 1,
            "role": "user",
            "content": "历史内容",
            "created_at": "2026-11-01T00:00:01+00:00",
        }],
    }

    first = await repo.import_desktop_history(**source)
    replay = await repo.import_desktop_history(**source)

    assert first == replay
    assert first["imported"] is True
    assert first["digest"] == "fca8c67f07208a4a4cf3149faf8cf7a23b3279257d5b46ef5aad3eea8195086c"
    session = await repo.get_session(UUID(source["sessions"][0]["id"]))
    assert session.metadata["title"] == "历史会话"
    assert session.project_id == "project-1"
    messages = await repo.list_visible_messages(session.id)
    assert [(message.id, message.content) for message in messages] == [("message-1", "历史内容")]


@pytest.mark.asyncio
async def test_desktop_history_import_rejects_changed_replay_and_rolls_back(tmp_path):
    repo = SessionRepository(tmp_path / "sessions.db")
    await repo.init()
    source = {
        "installation_id": "desktop-installation-1",
        "sessions": [{
            "id": "00000000-0000-0000-0000-000000000102",
            "title": "迁移会话",
            "project_id": None,
            "created_at": "2026-11-01T00:00:00+00:00",
        }],
        "messages": [{
            "id": "legacy-message-2",
            "session_id": "00000000-0000-0000-0000-000000000102",
            "sequence": 1,
            "role": "assistant",
            "content": "answer",
            "created_at": "2026-11-01T00:00:01+00:00",
        }],
    }
    await repo.import_desktop_history(**source)

    changed = {**source, "messages": [{**source["messages"][0], "content": "changed"}]}
    with pytest.raises(ValueError, match="desktop_history_conflict"):
        await repo.import_desktop_history(**changed)

    with pytest.raises(ValueError, match="desktop_history_digest_mismatch"):
        await repo.import_desktop_history(**source, source_digest="0" * 64)

    bad = {
        "installation_id": "desktop-installation-2",
        "sessions": [{**source["sessions"][0], "id": "bad-session"}],
        "messages": [{**source["messages"][0], "session_id": "bad-session"}],
    }
    with pytest.raises(ValueError, match="invalid_desktop_history"):
        await repo.import_desktop_history(**bad)
    assert await repo.get_session(UUID(source["sessions"][0]["id"])) is not None
    assert await repo.list_sessions(limit=20) == [await repo.get_session(UUID(source["sessions"][0]["id"]))]
