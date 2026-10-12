import json

import pytest
from pydantic import ValidationError

from app.agent.harness import Decision
from app.agent.harness_models import HarnessState
from app.agent.models import ContextState, LLMResponse
from app.assistant.strategy import AssistantStrategy


def state():
    return HarnessState(context_state=ContextState())


@pytest.mark.parametrize(
    "payload, expected_kind",
    [
        ({"protocol_version": 1, "type": "message", "payload": {"text": "阶段进展"}}, "continue"),
        ({"protocol_version": 1, "type": "ask_user", "payload": {"question": "选择？", "choices": ["A"]}}, "wait"),
        ({"protocol_version": 1, "type": "discover_tools", "payload": {"query": "知识"}}, "continue"),
        ({"protocol_version": 1, "type": "describe_tool", "payload": {"name": "read"}}, "continue"),
        ({"protocol_version": 1, "type": "call_tool", "payload": {
            "name": "read", "contract_version": "1", "arguments": {}}}, "call_tool"),
        ({"protocol_version": 1, "type": "finish", "payload": {"result": {"answer": "完成"}}}, "respond"),
    ],
)
async def test_protocol_decisions_map_to_runtime_control(payload, expected_kind):
    response = LLMResponse(kind="final", content=json.dumps(payload, ensure_ascii=False))

    decision = await AssistantStrategy().decide(state(), (), response)

    assert decision.kind == expected_kind


@pytest.mark.parametrize("content", [
    "plain text",
    "{not-json}",
    '{"protocol_version":1,"type":"finish","payload":{},"extra":true}',
    '{"protocol_version":2,"type":"finish","payload":{"result":{"answer":"x"}}}',
    '{"protocol_version":1,"type":"unknown","payload":{}}',
])
async def test_invalid_protocol_content_is_rejected(content):
    response = LLMResponse(kind="final", content=content)

    with pytest.raises((ValidationError, ValueError)):
        await AssistantStrategy().decide(state(), (), response)


async def test_legacy_final_without_protocol_decision_is_rejected():
    with pytest.raises((ValidationError, ValueError)):
        await AssistantStrategy().decide(
            state(), (), LLMResponse(kind="final", content="legacy answer")
        )


async def test_legacy_tool_call_is_not_silently_treated_as_protocol_decision():
    from app.agent.models import ToolCall

    response = LLMResponse(kind="tool_call", tool_call=ToolCall(name="read", arguments={}))

    with pytest.raises((ValidationError, ValueError)):
        await AssistantStrategy().decide(state(), (), response)


async def test_discovery_results_use_server_capability_registry():
    from app.assistant.capabilities import CapabilityRegistry
    from app.assistant.models import CapabilityDescriptor
    from app.agent.models import ToolDefinition

    class ReadTool:
        name = "read_file"

        def definition(self):
            return ToolDefinition(name=self.name, description="Read a file", parameters={"type": "object"})

    registry = CapabilityRegistry()
    registry.register(ReadTool(), CapabilityDescriptor(name="read_file", parameters={"type": "object"},
        requires_workspace=False, side_effect="read_only", recovery_class="read_only", available=True))
    result = AssistantStrategy().resolve_capability_decision(
        {"protocol_version": 1, "type": "discover_tools", "payload": {"query": "file"}}, registry)

    assert [item["name"] for item in result["tools"]] == ["read_file"]