from pathlib import Path
from uuid import uuid4

import pytest

from app.agent.config import RuntimeConfig
from app.agent.context import ContextManager
from app.agent.models import LLMResponse, ToolCall
from app.agent.runtime import AgentRuntime
from app.sessions.repository import SessionRepository
from app.tools.calculator import Calculator
from app.tools.registry import ToolRegistry


class FakeLLM:
    def __init__(self, *responses: LLMResponse):
        self.responses = list(responses)
        self.messages = []

    async def complete(self, messages, tools):
        self.messages.append((messages, tools))
        return self.responses.pop(0)


async def make_runtime(
    tmp_path: Path,
    llm: FakeLLM,
    config: RuntimeConfig | None = None,
) -> tuple[AgentRuntime, SessionRepository, object]:
    repository = SessionRepository(tmp_path / "runtime.db")
    await repository.init()
    session_id = await repository.create_session({"memory": ["persisted fact"]})
    runtime = AgentRuntime(
        llm=llm,
        context_manager=ContextManager(token_counter=len),
        tool_registry=ToolRegistry([Calculator()]),
        repository=repository,
        system_instruction="Be concise.",
        config=config,
    )
    return runtime, repository, session_id


@pytest.mark.asyncio
async def test_runtime_completes_and_persists_user_and_assistant_messages(tmp_path):
    llm = FakeLLM(LLMResponse(kind="final", content="done"))
    runtime, repository, session_id = await make_runtime(tmp_path, llm)

    result = await runtime.run(session_id, "hello")

    assert result.status == "completed"
    assert result.answer == "done"
    run = await repository.get_run(result.run_id, session_id)
    assert run is not None and run.status == "completed"
    events = await repository.list_events(result.run_id)
    assert [event.event_type for event in events] == [
        "run_started",
        "user_message",
        "assistant_message",
        "run_completed",
    ]
    assert [message.content for message in (await runtime._load_runtime_state(session_id, result.run_id))[0]] == [
        "hello",
        "done",
    ]


@pytest.mark.asyncio
async def test_runtime_executes_tool_then_completes(tmp_path):
    llm = FakeLLM(
        LLMResponse(
            kind="tool_call",
            tool_call=ToolCall(name="calculator", arguments={"expression": "2 + 3"}),
        ),
        LLMResponse(kind="final", content="The result is 5."),
    )
    runtime, repository, session_id = await make_runtime(tmp_path, llm)

    result = await runtime.run(session_id, "calculate")

    assert result.status == "completed"
    assert result.loop_count == 2
    events = await repository.list_events(result.run_id)
    assert [event.event_type for event in events] == [
        "run_started",
        "user_message",
        "assistant_message",
        "tool_result",
        "assistant_message",
        "run_completed",
    ]
    assert '"value": 5' in events[3].payload["content"]


@pytest.mark.asyncio
async def test_runtime_pauses_for_clarification(tmp_path):
    llm = FakeLLM(LLMResponse(kind="clarification", content="Which project?") )
    runtime, repository, session_id = await make_runtime(tmp_path, llm)

    result = await runtime.run(session_id, "continue")

    assert result.status == "paused"
    assert result.answer == "Which project?"
    run = await repository.get_run(result.run_id, session_id)
    assert run is not None and run.status == "paused"


@pytest.mark.asyncio
async def test_runtime_persists_failure_when_loop_budget_is_exceeded(tmp_path):
    llm = FakeLLM(
        LLMResponse(
            kind="tool_call",
            tool_call=ToolCall(name="calculator", arguments={"expression": "1 + 1"}),
        ),
        LLMResponse(kind="final", content="unreachable"),
    )
    runtime, repository, session_id = await make_runtime(
        tmp_path,
        llm,
        RuntimeConfig(max_loops=1),
    )

    result = await runtime.run(session_id, "calculate")

    assert result.status == "failed"
    assert result.error is not None
    assert result.error["type"] == "RuntimeError"
    run = await repository.get_run(result.run_id, session_id)
    assert run is not None and run.status == "failed"


@pytest.mark.asyncio
async def test_runtime_replays_messages_when_resuming_from_checkpoint(tmp_path):
    llm = FakeLLM(LLMResponse(kind="final", content="new answer"))
    runtime, repository, session_id = await make_runtime(tmp_path, llm)
    run_id = await repository.create_run(session_id)
    await repository.append_event(
        session_id,
        run_id,
        "user_message",
        {"content": "old question"},
    )
    sequence = await repository.append_event(
        session_id,
        run_id,
        "assistant_message",
        {"content": "old answer"},
    )
    await repository.save_checkpoint(
        run_id,
        sequence,
        {
            "context": {"summary": "compacted"},
            "counters": {"loop_count": 1},
        },
    )

    messages, state, counters = await runtime._load_runtime_state(session_id, run_id)

    assert [message.content for message in messages] == ["old question", "old answer"]
    assert state.summary == "compacted"
    assert counters.loop_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_runtime_rejects_resuming_terminal_run(tmp_path, status):
    llm = FakeLLM(LLMResponse(kind="final", content="unused"))
    runtime, repository, session_id = await make_runtime(tmp_path, llm)
    run_id = await repository.create_run(session_id)
    await repository.update_run(run_id, status)

    with pytest.raises(ValueError, match="terminal and cannot be resumed"):
        await runtime.run(session_id, "retry", run_id=run_id)


@pytest.mark.asyncio
async def test_runtime_persists_user_message_as_content(tmp_path):
    llm = FakeLLM(LLMResponse(kind="final", content="done"))
    runtime, repository, session_id = await make_runtime(tmp_path, llm)

    result = await runtime.run(session_id, "hello")
    events = await repository.list_events(result.run_id)

    user_event = next(event for event in events if event.event_type == "user_message")
    assert user_event.payload["content"] == "hello"
    assert "message" not in user_event.payload


@pytest.mark.asyncio
async def test_new_run_receives_previous_completed_turn_and_is_session_isolated(tmp_path):
    llm = FakeLLM(
        LLMResponse(kind="final", content="你的博客主题是 Redis。"),
        LLMResponse(kind="final", content="可以继续讨论 Redis 持久化。"),
        LLMResponse(kind="final", content="新的会话。"),
    )
    runtime, repository, session_id = await make_runtime(tmp_path, llm)

    first = await runtime.run(session_id, "我的博客主题是 Redis。")
    second = await runtime.run(session_id, "继续这个主题。")

    assert first.status == "completed"
    assert second.status == "completed"
    contents = [message.content for message in llm.messages[1][0]]
    assert "我的博客主题是 Redis。" in contents
    assert "你的博客主题是 Redis。" in contents
    assert contents.count("继续这个主题。") == 1

    other_session = await repository.create_session()
    await runtime.run(other_session, "你好。")
    other_contents = [message.content for message in llm.messages[2][0]]
    assert "我的博客主题是 Redis。" not in other_contents
    assert "你的博客主题是 Redis。" not in other_contents


@pytest.mark.asyncio
async def test_resuming_current_run_does_not_duplicate_messages(tmp_path):
    llm = FakeLLM(LLMResponse(kind="final", content="恢复后的回答。"))
    runtime, repository, session_id = await make_runtime(tmp_path, llm)
    run_id = await repository.create_run(session_id)
    await repository.append_event(session_id, run_id, "run_started", {})
    await repository.append_event(
        session_id, run_id, "user_message", {"content": "当前问题"}
    )
    await repository.update_run(run_id, "paused")

    result = await runtime.run(session_id, run_id=run_id)

    assert result.status == "completed"
    contents = [message.content for message in llm.messages[0][0]]
    assert contents.count("当前问题") == 1
    assert "恢复后的回答。" not in contents


@pytest.mark.asyncio
async def test_previous_failed_or_paused_runs_are_not_inherited(tmp_path):
    llm = FakeLLM(LLMResponse(kind="final", content="只应看到已完成。"))
    runtime, repository, session_id = await make_runtime(tmp_path, llm)

    completed = await runtime.run(session_id, "已完成问题")
    failed_run = await repository.create_run(session_id)
    await repository.append_event(
        session_id, failed_run, "user_message", {"content": "失败问题"}
    )
    await repository.append_event(
        session_id, failed_run, "assistant_message", {"content": "失败回答"}
    )
    await repository.update_run(failed_run, "failed")
    paused_run = await repository.create_run(session_id)
    await repository.append_event(
        session_id, paused_run, "user_message", {"content": "暂停问题"}
    )
    await repository.update_run(paused_run, "paused")

    await runtime.run(session_id, "新问题")
    contents = [message.content for message in llm.messages[1][0]]
    assert "已完成问题" in contents
    assert "失败问题" not in contents
    assert "暂停问题" not in contents
    assert completed.status == "completed"
