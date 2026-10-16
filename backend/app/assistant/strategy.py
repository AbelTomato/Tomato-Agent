"""Parse strict assistant protocol decisions at the Runtime boundary."""

import json
from typing import Any
from uuid import uuid4

from pydantic import TypeAdapter

from app.agent.harness import Decision
from app.runs.models import PendingAction
from .models import (
    AssistantDecision,
    AskUserDecision,
    CallToolDecision,
    DescribeToolDecision,
    DiscoverToolsDecision,
    FinishDecision,
    MessageDecision,
)

_DECISION_ADAPTER = TypeAdapter(AssistantDecision)


def assistant_decision_json_schema() -> dict[str, Any]:
    """Build a strict-provider schema while preserving runtime v1's dynamic JSON fields."""
    schema = _DECISION_ADAPTER.json_schema()
    definitions = schema.pop("$defs")

    def resolve(node):
        if isinstance(node, list):
            return [resolve(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            return resolve(definitions[name])
        result = {key: resolve(value) for key, value in node.items()
                  if key not in {"title", "default", "discriminator", "oneOf"}}
        if "const" in result:
            result["enum"] = [result.pop("const")]
        if result.get("type") == "object":
            properties = result.get("properties", {})
            result["additionalProperties"] = False
            result["required"] = list(properties)
        return result

    branches = [resolve(definitions[item["$ref"].rsplit("/", 1)[-1]]) for item in schema["oneOf"]]
    payload_schemas = [branch["properties"]["payload"] for branch in branches]
    payload_schemas[4]["properties"]["arguments"] = {
        "type": "string", "description": "JSON-encoded tool arguments object"
    }
    finish_result = payload_schemas[5]["properties"]["result"]
    test_results = finish_result["properties"]["test_results"]
    test_results["items"] = {"type": "string", "description": "JSON-encoded test result object"}
    decision_schemas = []
    for branch in branches:
        decision_type = branch["properties"]["type"]["enum"][0]
        decision_schemas.append({
            "type": "object",
            "properties": {
                "protocol_version": {"type": "integer", "enum": [1]},
                "type": {"type": "string", "enum": [decision_type]},
                "payload": branch["properties"]["payload"],
            },
            "required": ["protocol_version", "type", "payload"],
            "additionalProperties": False,
        })
    return {
        "type": "object",
        "properties": {"decision": {"anyOf": decision_schemas}},
        "required": ["decision"],
        "additionalProperties": False,
    }


ASSISTANT_DECISION_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "assistant_decision_v1",
        "strict": True,
        "schema": assistant_decision_json_schema(),
    },
}


class AssistantStrategy:
    async def decide(self, state, available_tools, llm_response=None):
        if llm_response is None or llm_response.kind != "final" or not llm_response.content:
            raise ValueError("assistant_protocol_decision_required")
        try:
            raw = json.loads(llm_response.content)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("invalid_assistant_protocol_json") from exc
        decision = _DECISION_ADAPTER.validate_json(llm_response.content)
        if isinstance(decision, MessageDecision):
            return Decision(kind="continue", output=decision.payload.text)
        if isinstance(decision, AskUserDecision):
            return Decision(kind="wait", pending_action=PendingAction(
                action_id=str(uuid4()), reason=decision.payload.question, business_version=0,
                action_type="clarification", allowed_actions=["provide_input", "cancel"],
                display_data=decision.payload.model_dump(mode="json"),
                frozen_parameters={"choices": list(decision.payload.choices), "allow_free_text": True}))
        if isinstance(decision, CallToolDecision):
            return Decision(kind="call_tool", tool_name=decision.payload.name,
                            arguments=decision.payload.arguments, contract_version=decision.payload.contract_version)
        if isinstance(decision, FinishDecision):
            return Decision(kind="respond", output=decision.payload.result.model_dump(mode="json"))
        if isinstance(decision, (DiscoverToolsDecision, DescribeToolDecision)):
            return Decision(kind="continue", output=decision.model_dump(mode="json"))
        raise ValueError("unsupported_assistant_protocol_decision")

    def resolve_capability_decision(self, value: dict[str, Any], capabilities) -> dict[str, Any]:
        decision = _DECISION_ADAPTER.validate_python(value)
        if isinstance(decision, DiscoverToolsDecision):
            descriptors = capabilities.catalog(category=decision.payload.category, query=decision.payload.query)
            return {"type": decision.type, "tools": descriptors}
        if isinstance(decision, DescribeToolDecision):
            tool = next((item for item in capabilities.catalog()
                         if item["name"] == decision.payload.name), None)
            if tool is None:
                return {"type": decision.type, "error": {"code": "unknown_tool"}}
            return {"type": decision.type, "tool": tool,
                    "descriptor": capabilities.resolve(decision.payload.name).model_dump(mode="json")}
        raise ValueError("not_a_capability_discovery_decision")