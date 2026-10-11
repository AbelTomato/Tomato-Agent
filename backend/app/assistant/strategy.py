"""Translate provider proposals; authority remains at server boundaries."""

from uuid import uuid4

from app.agent.harness import Decision
from app.runs.models import PendingAction


class AssistantStrategy:
    async def decide(self, state, available_tools, llm_response=None):
        if llm_response is not None and llm_response.kind == "final":
            return Decision(kind="respond", output=llm_response.content or "")
        call = llm_response.tool_call if llm_response is not None else None
        if call is None:
            return Decision(kind="stop", reason="invalid_model_response")
        if call.name == "request_input":
            from .tools.project import ClarificationInput
            arguments = ClarificationInput.model_validate(call.arguments)
            return Decision(kind="wait", pending_action=PendingAction(
                action_id=str(uuid4()), reason=arguments.message, business_version=0,
                action_type="clarification", allowed_actions=["provide_input", "reject_action"]))
        return Decision(kind="call_tool", tool_name=call.name, arguments=call.arguments)