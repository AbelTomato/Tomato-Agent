import json

import pytest

from app.agent.models import LLMResponse, Message, ToolDefinition
from app.llm.budgeted_client import (
    BudgetedRecordingLLMClient,
    ProviderCallBudgetExceeded,
)
from evals.harness_stage2 import FixedTask, run_real_provider_comparison


class FakeLLM:
    def __init__(self, responses: list[LLMResponse | Exception]) -> None:
        self.responses = responses
        self.calls = 0
        self.last_token_usage: dict[str, int] | None = None

    async def complete(
        self, messages: list[Message], tools: list[ToolDefinition]
    ) -> LLMResponse:
        self.calls += 1
        response = self.responses[self.calls - 1]
        if isinstance(response, Exception):
            raise response
        self.last_token_usage = {"prompt_tokens": 7, "completion_tokens": 3}
        return response


@pytest.mark.asyncio
async def test_budgeted_client_records_success_and_usage_without_secrets(tmp_path):
    output = tmp_path / "provider-responses.jsonl"
    fake = FakeLLM([LLMResponse(kind="final", content="provider answer")])
    client = BudgetedRecordingLLMClient(fake, max_calls=1, output_path=output)

    result = await client.complete([Message(role="user", content="question")], [])

    assert result.content == "provider answer"
    assert client.call_count == 1
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["call_index"] == 1
    assert record["response"] == {"kind": "final", "content": "provider answer", "tool_call": None}
    assert record["token_usage"] == {"prompt_tokens": 7, "completion_tokens": 3}
    assert "Authorization" not in record


@pytest.mark.asyncio
async def test_budget_is_strict_and_failed_attempts_consume_calls(tmp_path):
    output = tmp_path / "provider-responses.jsonl"
    fake = FakeLLM([RuntimeError("secret provider detail"), LLMResponse(kind="final", content="ok")])
    client = BudgetedRecordingLLMClient(fake, max_calls=1, output_path=output)

    with pytest.raises(RuntimeError, match="provider request failed"):
        await client.complete([], [])

    with pytest.raises(ProviderCallBudgetExceeded, match="maximum of 1"):
        await client.complete([], [])

    assert fake.calls == 1
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert records[0]["error"] == "provider request failed"
    assert "secret provider detail" not in output.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_real_provider_comparison_uses_one_bounded_call_per_case(tmp_path):
    fake = FakeLLM(
        [LLMResponse(kind="final", content=f"answer-{index}") for index in range(4)]
    )
    tasks = (
        FixedTask(
            task_id="research-1",
            task_type="research",
            input_text="research question",
            evidence_snapshot="evidence",
            expected_evidence_refs=("ref",),
        ),
    )

    report = await run_real_provider_comparison(
        tasks,
        model_id="test-model",
        prompt_version="prompt-v1",
        runtime_version="runtime-v1",
        provider=fake,
        max_calls=2,
        response_path=tmp_path / "responses.jsonl",
    )

    assert fake.calls == 2
    assert report["provenance"]["provider"] == "real-compatible"
    assert report["provenance"]["external_requests"] is True
    assert len(report["cases"]) == 2
    assert report["metrics"]["model_calls"] == 2