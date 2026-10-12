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


class AssistantStrategy:
    async def decide(self, state, available_tools, llm_response=None):
        if llm_response is None or llm_response.kind != "final" or not llm_response.content:
            raise ValueError("assistant_protocol_decision_required")
        try:
            raw = json.loads(llm_response.content)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("invalid_assistant_protocol_json") from exc
        decision = _DECISION_ADAPTER.validate_python(raw)
        if isinstance(decision, MessageDecision):
            return Decision(kind="continue", output=decision.payload.text)
        if isinstance(decision, AskUserDecision):
            return Decision(kind="wait", pending_action=PendingAction(
                action_id=str(uuid4()), reason=decision.payload.question, business_version=0,
                action_type="clarification", allowed_actions=["provide_input", "reject_action"]))
        if isinstance(decision, CallToolDecision):
            return Decision(kind="call_tool", tool_name=decision.payload.name,
                            arguments=decision.payload.arguments)
        if isinstance(decision, FinishDecision):
            return Decision(kind="respond", output=decision.payload.result.model_dump(mode="json"))
        if isinstance(decision, (DiscoverToolsDecision, DescribeToolDecision)):
            return Decision(kind="continue", output=decision.model_dump(mode="json"))
        raise ValueError("unsupported_assistant_protocol_decision")

    def resolve_capability_decision(self, value: dict[str, Any], capabilities) -> dict[str, Any]:
        decision = _DECISION_ADAPTER.validate_python(value)
        if isinstance(decision, DiscoverToolsDecision):
            if decision.payload.category is not None:
                raise ValueError("assistant_capability_categories_not_available")
            descriptors = [
                {**tool.model_dump(mode="json"), **capabilities.resolve(tool.name).model_dump(mode="json")}
                for tool in capabilities.definitions()
                if (decision.payload.query is None
                    or decision.payload.query.casefold() in tool.name.casefold()
                    or decision.payload.query.casefold() in tool.description.casefold())
            ]
            return {"type": decision.type, "tools": descriptors}
        if isinstance(decision, DescribeToolDecision):
            tool = next((item for item in capabilities.definitions()
                         if item.name == decision.payload.name), None)
            if tool is None:
                raise ValueError("assistant_capability_unavailable")
            return {"type": decision.type, "tool": tool.model_dump(mode="json"),
                    "descriptor": capabilities.resolve(tool.name).model_dump(mode="json")}
        raise ValueError("not_a_capability_discovery_decision")