"""Explicitly synthetic decisions for offline Runtime/UI contract checks."""

from copy import deepcopy
import json

from pydantic import TypeAdapter

from app.agent.models import LLMResponse
from app.assistant.models import AssistantDecision
from app.assistant.strategy import ASSISTANT_DECISION_RESPONSE_FORMAT
from app.llm.compatible_client import _REQUEST_CORRELATION


STAGE_MESSAGE = "Scripted runtime: preparing a read-only inspection."
QUESTION = "Which file should I explain?"
RESUMED_MESSAGE = "Scripted runtime: clarification received; reading cache.py."
FINAL_ANSWER = "cache.py defines CACHE_TTL = 60. No tests were executed."


def decision(kind, payload):
    return {"protocol_version": 1, "type": kind, "payload": payload}


def clarification_script():
    return [
        decision("message", {"text": STAGE_MESSAGE}),
        decision("ask_user", {"question": QUESTION, "choices": ["cache.py"], "allow_free_text": True}),
        decision("message", {"text": RESUMED_MESSAGE}),
        decision("call_tool", {"name": "read_file", "contract_version": "1", "arguments": {"path": "cache.py"}}),
        decision("finish", {"result": {"answer": FINAL_ANSWER}}),
    ]


class ScriptedRuntimeClient:
    """Finite per-Run scripts; never owns or falls back to a network client."""

    mode = "scripted"
    timeout = 5

    def __init__(self, decisions=None):
        self.decisions = deepcopy(clarification_script() if decisions is None else decisions)
        self.calls_by_run = {}
        self.observations = []

    @property
    def calls(self):
        return sum(self.calls_by_run.values())

    async def complete(self, messages, tools, *, response_format=None):
        if tools or response_format != ASSISTANT_DECISION_RESPONSE_FORMAT:
            raise ValueError("scripted_runtime_requires_decision_protocol")
        correlation = _REQUEST_CORRELATION.get() or {}
        run_id = correlation.get("run_id")
        if not run_id:
            raise ValueError("scripted_runtime_requires_run_identity")
        index = self.calls_by_run.get(run_id, 0)
        if index >= len(self.decisions):
            raise RuntimeError("scripted_runtime_exhausted")
        raw = deepcopy(self.decisions[index])
        parsed = TypeAdapter(AssistantDecision).validate_json(json.dumps(raw))
        raw = parsed.model_dump(mode="json")
        self.calls_by_run[run_id] = index + 1
        self.observations.append({"mode": self.mode, "run_id": str(run_id),
                                  "index": index + 1, "decision_type": raw["type"]})
        if raw["type"] == "call_tool":
            raw["payload"]["arguments"] = json.dumps(raw["payload"]["arguments"])
        elif raw["type"] == "finish":
            result = raw["payload"]["result"]
            result["test_results"] = [json.dumps(item) for item in result["test_results"]]
        return LLMResponse(kind="final", content=json.dumps({"decision": raw}))