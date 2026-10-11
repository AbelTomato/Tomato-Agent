import pytest

from app.agent.models import LLMResponse, ToolCall
from app.assistant.strategy import AssistantStrategy


async def test_strategy_translates_function_call_and_final():
    strategy = AssistantStrategy()
    call = await strategy.decide(None, [], LLMResponse(kind="tool_call", tool_call=ToolCall(name="read_file", arguments={"path": "a.py"})))
    assert call.kind == "call_tool" and call.arguments == {"path": "a.py"}
    final = await strategy.decide(None, [], LLMResponse(kind="final", content="hello"))
    assert final.kind == "respond" and final.output == "hello"


async def test_strategy_clarification_is_not_business_authorization():
    decision = await AssistantStrategy().decide(None, [], LLMResponse(kind="tool_call", tool_call=ToolCall(name="request_input", arguments={"message": "Which topic?"})))
    assert decision.kind == "wait"
    assert decision.pending_action.action_type == "clarification"
    assert "approve_action" not in decision.pending_action.allowed_actions