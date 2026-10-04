from dataclasses import dataclass

import pytest

from app.agent.harness import Decision, Harness
from app.agent.harness_models import Budget, HarnessState, ToolPolicy
from app.agent.models import ContextState, LLMResponse
from app.tools.registry import ToolRegistry


class FakeLLM:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        return LLMResponse(kind="final", content="fake model observation")


@dataclass
class FakeTaskStrategy:
    task_kind: str

    async def decide(self, state, available_tools):
        assert state.structured_state["task_kind"] == self.task_kind
        assert available_tools == ()
        return Decision(kind="respond", output={"task_kind": self.task_kind, "ok": True})


@pytest.mark.asyncio
async def test_same_harness_contract_drives_fake_research_and_writing_tasks():
    budget = Budget(
        max_loops=2,
        max_tool_calls=1,
        max_duration_seconds=1.0,
        max_context_tokens=1_000,
        max_response_chars=100,
    )
    policy = ToolPolicy(
        allowed_tools=frozenset(),
        max_calls=1,
        timeout_seconds=0.2,
        max_result_chars=100,
    )
    llm = FakeLLM()
    harness = Harness()

    results = []
    for task_kind in ("research", "writing"):
        state = HarnessState(
            task_id=f"{task_kind}-task",
            structured_state={"task_kind": task_kind},
            context_state=ContextState(),
        )
        results.append(
            await harness.run(
                state,
                FakeTaskStrategy(task_kind),
                llm=llm,
                tools=ToolRegistry(),
                policy=policy,
                budget=budget,
            )
        )

    assert [result.status for result in results] == ["completed", "completed"]
    assert [result.output["task_kind"] for result in results] == ["research", "writing"]
    assert [result.usage.model_calls for result in results] == [1, 1]
    assert llm.calls == 2
    assert [event.event_type for event in harness.audit_events] == [
        "harness.started",
        "harness.started",
    ]