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


def test_discovery_empty_filters_recover_cross_capability_catalog(tmp_path):
    from app.assistant.capabilities import CapabilityRegistry
    from app.assistant.service import INSTRUCTION
    from app.assistant.tools.knowledge import register_knowledge
    from app.assistant.tools.project import register_project_reads
    from test_application_lifecycle import make_app

    app = make_app(tmp_path)
    registry = CapabilityRegistry()
    register_project_reads(registry, app.state.workspace_service, app.state.artifact_service, app.state.run_repository)
    register_knowledge(registry, app.state.knowledge_service)
    strategy = AssistantStrategy()
    query = ("Find local Redis SETEX reference material and registered project file cache.py. "
             "Read only; no tests.")
    for category in ("knowledge and project files", "knowledge"):
        result = strategy.resolve_capability_decision(
            {"protocol_version": 1, "type": "discover_tools", "payload": {"category": category, "query": query}},
            registry)
        assert result["tools"] == []
    result = strategy.resolve_capability_decision(
        {"protocol_version": 1, "type": "discover_tools", "payload": {"category": None, "query": None}}, registry)
    names = {item["name"] for item in result["tools"]}
    assert {"read_file", "search_knowledge", "read_knowledge"} <= names
    assert "过滤结果为空时先将两个条件设为 null 重试" in INSTRUCTION


def test_assistant_decision_json_schema_is_strict_compatible_and_covers_protocol():
    from app.assistant.strategy import assistant_decision_json_schema

    schema = assistant_decision_json_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    branches = schema["properties"]["decision"]["anyOf"]
    assert {branch["properties"]["type"]["enum"][0] for branch in branches} == {
        "message", "ask_user", "discover_tools", "describe_tool", "call_tool", "finish"
    }
    assert all(branch["additionalProperties"] is False for branch in branches)
    call_tool = next(item for item in branches if item["properties"]["type"]["enum"] == ["call_tool"])
    assert call_tool["properties"]["payload"]["properties"]["arguments"] == {
        "type": "string", "description": "JSON-encoded tool arguments object"
    }
    finish = next(item for item in branches if item["properties"]["type"]["enum"] == ["finish"])
    assert finish["properties"]["payload"]["properties"]["result"]["properties"]["test_results"]["items"] == {
        "type": "string", "description": "JSON-encoded test result object"
    }
    discover = next(item for item in branches if item["properties"]["type"]["enum"] == ["discover_tools"])
    discover_payload = discover["properties"]["payload"]["properties"]
    for field in ("category", "query"):
        assert discover_payload[field] == {
            "anyOf": [{"type": "string"}, {"type": "null"}]
        }

    def check_objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node.get("required", [])) == set(node.get("properties", {}))
            for child in node.values():
                check_objects(child)
        elif isinstance(node, list):
            for child in node:
                check_objects(child)

    check_objects(schema)


async def test_assistant_protocol_client_requests_schema_and_restores_dynamic_json_fields():
    import json
    from app.agent.models import LLMResponse
    from app.assistant.service import AssistantProtocolClient
    from app.assistant.strategy import ASSISTANT_DECISION_RESPONSE_FORMAT

    class Client:
        async def complete(self, messages, tools, *, response_format=None):
            assert response_format == ASSISTANT_DECISION_RESPONSE_FORMAT
            return LLMResponse(kind="final", content=json.dumps({
                "decision": {
                    "protocol_version": 1,
                    "type": "call_tool",
                    "payload": {"name": "read_file", "contract_version": "1", "arguments": "{\"path\":\"a.py\"}"},
                },
            }))

    response = await AssistantProtocolClient(Client()).complete([], [],
        response_format=ASSISTANT_DECISION_RESPONSE_FORMAT)
    assert json.loads(response.content)["payload"]["arguments"] == {"path": "a.py"}


async def test_assistant_protocol_client_uses_only_first_complete_json_object():
    import json
    from app.agent.models import LLMResponse
    from app.assistant.service import AssistantProtocolClient
    from app.assistant.strategy import ASSISTANT_DECISION_RESPONSE_FORMAT

    class Client:
        async def complete(self, messages, tools, *, response_format=None):
            assert response_format == ASSISTANT_DECISION_RESPONSE_FORMAT
            first = json.dumps({"decision": {
                "protocol_version": 1,
                "type": "message",
                "payload": {"text": "The literal braces { and } are data."},
            }})
            second = json.dumps({"decision": {
                "protocol_version": 1,
                "type": "finish",
                "payload": {"result": {"answer": "must not execute"}},
            }})
            return LLMResponse(kind="final", content=" \n" + first + second)

    response = await AssistantProtocolClient(Client()).complete(
        [], [], response_format=ASSISTANT_DECISION_RESPONSE_FORMAT)
    assert json.loads(response.content) == {
        "protocol_version": 1,
        "type": "message",
        "payload": {"text": "The literal braces { and } are data."},
    }


async def test_assistant_protocol_client_rejects_non_json_prefix_before_object():
    from app.agent.models import LLMResponse
    from app.assistant.service import AssistantProtocolClient
    from app.assistant.strategy import ASSISTANT_DECISION_RESPONSE_FORMAT

    class Client:
        async def complete(self, messages, tools, *, response_format=None):
            return LLMResponse(kind="final", content='note: {"decision": {}}')

    with pytest.raises(RuntimeError, match="invalid_assistant_protocol_json"):
        await AssistantProtocolClient(Client()).complete(
            [], [], response_format=ASSISTANT_DECISION_RESPONSE_FORMAT)


async def test_assistant_protocol_client_rejects_incomplete_first_object():
    from app.agent.models import LLMResponse
    from app.assistant.service import AssistantProtocolClient
    from app.assistant.strategy import ASSISTANT_DECISION_RESPONSE_FORMAT

    class Client:
        async def complete(self, messages, tools, *, response_format=None):
            return LLMResponse(kind="final", content='{"decision":{"type":"message"')

    with pytest.raises(RuntimeError, match="invalid_assistant_protocol_json"):
        await AssistantProtocolClient(Client()).complete(
            [], [], response_format=ASSISTANT_DECISION_RESPONSE_FORMAT)


async def test_assistant_protocol_client_rejects_invalid_encoded_fields():
    from app.agent.models import LLMResponse
    from app.assistant.service import AssistantProtocolClient
    from app.assistant.strategy import ASSISTANT_DECISION_RESPONSE_FORMAT

    class Client:
        async def complete(self, messages, tools, *, response_format=None):
            assert response_format == ASSISTANT_DECISION_RESPONSE_FORMAT
            return LLMResponse(kind="final", content='{"decision":{"type":"call_tool","payload":{"arguments":"not json"}}}')

    with pytest.raises(RuntimeError, match="invalid_assistant_protocol_json"):
        await AssistantProtocolClient(Client()).complete(
            [], [], response_format=ASSISTANT_DECISION_RESPONSE_FORMAT
        )