from time import monotonic

import pytest

from app.agent.config import RuntimeConfig
from app.agent.context import ContextManager
from app.agent.loop import AgentLoop
from app.agent.models import ContextState, LLMResponse, Message, ToolCall, ToolDefinition


class FakeLLM:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def complete(self, messages, tools):
        self.calls.append((messages, tools))
        return self.response


@pytest.mark.asyncio
async def test_agent_loop_returns_validated_tool_decision_without_storage():
    response = LLMResponse(
        kind="tool_call",
        tool_call=ToolCall(name="lookup", arguments={"key": "value"}),
    )
    llm = FakeLLM(response)
    loop = AgentLoop(
        llm,
        ContextManager(token_counter=len),
        "system",
        RuntimeConfig(),
        [ToolDefinition(name="lookup", description="read", parameters={})],
    )

    result = await loop.next_response(
        [Message(role="user", content="question")],
        ContextState(),
        loop_count=0,
        tool_call_count=0,
        started_at=monotonic(),
        persisted_elapsed=0.0,
    )

    assert result.response == response
    assert result.loop_count == 1
    assert llm.calls[0][1][0].name == "lookup"
    assert llm.calls[0][0][0].role == "system"


@pytest.mark.asyncio
async def test_agent_loop_rejects_exhausted_budget_before_llm_call():
    llm = FakeLLM(LLMResponse(kind="final", content="unused"))
    loop = AgentLoop(
        llm,
        ContextManager(token_counter=len),
        "system",
        RuntimeConfig(max_loops=1),
        [],
    )

    with pytest.raises(RuntimeError, match="Maximum loop count exceeded"):
        await loop.next_response(
            [],
            ContextState(),
            loop_count=1,
            tool_call_count=0,
            started_at=0.0,
            persisted_elapsed=0.0,
        )
    assert llm.calls == []