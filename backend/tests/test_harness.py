import asyncio
from dataclasses import dataclass

import pytest
from pydantic import BaseModel

from app.agent.harness import Harness
from app.agent.harness_models import Budget, HarnessState, ToolPolicy
from app.agent.models import ContextState, LLMResponse, ToolDefinition
from app.tools.base import ToolContext, ToolResult
from app.tools.registry import ToolRegistry


class AddInput(BaseModel):
    value: int


class FakeTool:
    name = "add"
    description = "add"
    input_model = AddInput

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=AddInput.model_json_schema(),
        )

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        data = self.input_model.model_validate(arguments)
        return ToolResult(success=True, data={"value": data.value + 1})


class FailingTool(FakeTool):
    name = "fail"

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        return ToolResult(success=False, error="internal detail")


class FakeLLM:
    def __init__(self, responses: list[LLMResponse], delay: float = 0) -> None:
        self.responses = responses
        self.delay = delay
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]


@dataclass
class ScriptedStrategy:
    decisions: list

    async def decide(self, state, available_tools):
        return self.decisions.pop(0)


def state() -> HarnessState:
    return HarnessState(context_state=ContextState())


def budget(**overrides) -> Budget:
    values = dict(
        max_loops=3,
        max_tool_calls=3,
        max_duration_seconds=1,
        max_context_tokens=1_000,
        max_response_chars=100,
    )
    values.update(overrides)
    return Budget(**values)


def policy(**overrides) -> ToolPolicy:
    values = dict(allowed_tools=frozenset({"add"}), max_calls=3,
                  timeout_seconds=0.2, max_result_chars=100)
    values.update(overrides)
    return ToolPolicy(**values)


@pytest.mark.asyncio
async def test_harness_completes_and_preserves_model_usage():
    llm = FakeLLM([LLMResponse(kind="final", content="done")])
    result = await Harness().run(
        state(), ScriptedStrategy([{"kind": "respond", "output": "done"}]),
        llm=llm, tools=ToolRegistry(), policy=policy(), budget=budget(),
    )
    assert result.status == "completed"
    assert result.output == "done"
    assert result.usage.model_calls == 1


@pytest.mark.asyncio
async def test_harness_active_stop_keeps_consumed_usage():
    llm = FakeLLM([LLMResponse(kind="final", content="observation")])
    result = await Harness().run(
        state(), ScriptedStrategy([{"kind": "stop", "reason": "enough"}]),
        llm=llm, tools=ToolRegistry(), policy=policy(), budget=budget(),
    )
    assert result.status == "stopped"
    assert result.stop_reason == "enough"
    assert result.usage.model_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("decisions, expected", [
    ([{"kind": "respond", "output": "x"}], "completed"),
])
async def test_harness_normal_response(decisions, expected):
    result = await Harness().run(
        state(), ScriptedStrategy(decisions),
        llm=FakeLLM([LLMResponse(kind="final", content="x")]),
        tools=ToolRegistry(), policy=policy(), budget=budget(),
    )
    assert result.status == expected


@pytest.mark.asyncio
async def test_harness_loop_limit_and_tool_limit():
    loop_result = await Harness().run(
        state(), ScriptedStrategy([
            {"kind": "call_tool", "tool_name": "add", "arguments": {"value": 1}},
            {"kind": "call_tool", "tool_name": "add", "arguments": {"value": 1}},
        ]),
        llm=FakeLLM([LLMResponse(kind="tool_call")]),
        tools=ToolRegistry([FakeTool()]), policy=policy(), budget=budget(max_loops=2),
    )
    assert loop_result.status == "stopped"
    assert "loop" in (loop_result.stop_reason or "")

    tool_result = await Harness().run(
        state(), ScriptedStrategy([
            {"kind": "call_tool", "tool_name": "add", "arguments": {"value": 1}},
            {"kind": "call_tool", "tool_name": "add", "arguments": {"value": 1}},
        ]),
        llm=FakeLLM([LLMResponse(kind="tool_call")]),
        tools=ToolRegistry([FakeTool()]), policy=policy(), budget=budget(max_tool_calls=1),
    )
    assert tool_result.status == "stopped"
    assert "tool" in (tool_result.stop_reason or "")
    assert tool_result.usage.model_calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [
    {"kind": "call_tool", "tool_name": "missing", "arguments": {}},
    {"kind": "call_tool", "tool_name": "add", "arguments": {"bad": 1}},
    {"kind": "call_tool", "tool_name": "fail", "arguments": {"value": 1}},
])
async def test_harness_rejects_invalid_or_failed_tool(decision):
    result = await Harness().run(
        state(), ScriptedStrategy([decision]),
        llm=FakeLLM([LLMResponse(kind="tool_call")]),
        tools=ToolRegistry([FakeTool(), FailingTool()]),
        policy=policy(), budget=budget(),
    )
    assert result.status == "failed"
    assert result.usage.model_calls == 1
    assert result.output is None


@pytest.mark.asyncio
async def test_harness_enforces_response_and_duration_limits():
    too_long = await Harness().run(
        state(), ScriptedStrategy([{"kind": "respond", "output": "0123456789"}]),
        llm=FakeLLM([LLMResponse(kind="final", content="observation")]),
        tools=ToolRegistry(), policy=policy(), budget=budget(max_response_chars=3),
    )
    assert too_long.status == "failed"
    assert "output" in (too_long.stop_reason or "")

    timed_out = await Harness().run(
        state(), ScriptedStrategy([{"kind": "stop", "reason": "late"}]),
        llm=FakeLLM([LLMResponse(kind="final", content="x")], delay=0.05),
        tools=ToolRegistry(), policy=policy(), budget=budget(max_duration_seconds=0.01),
    )
    assert timed_out.status == "stopped"
    assert "duration" in (timed_out.stop_reason or "")