from __future__ import annotations

from uuid import uuid4

import pytest

from app.agent.models import LLMResponse, Message, ToolCall, ToolDefinition
from app.observability.rag_trace import canonical_json_sha256
from app.observability.recording_llm_client import (
    LLMObservationError,
    RecordingLLMClient,
    llm_prompt_scope,
    llm_question_scope,
    make_prompt_identity,
)


class EventCollector:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def emit(self, event_type, *, status, payload, duration_ms) -> None:
        self.events.append(
            (
                event_type,
                {
                    "status": status,
                    "payload": payload,
                    "duration_ms": duration_ms,
                },
            )
        )


class FakeLLM:
    def __init__(self, responses: list[LLMResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[list[Message], list[ToolDefinition]]] = []

    async def complete(self, messages, tools):
        self.calls.append((messages, tools))
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, Exception):
            raise response
        return response


def make_recording_client(fake: FakeLLM, collector: EventCollector) -> RecordingLLMClient:
    return RecordingLLMClient(
        fake,
        observer=collector,
        run_id=uuid4(),
        model_id="offline-test-model",
    )


@pytest.mark.asyncio
async def test_recording_client_forwards_exact_messages_and_tools_and_records_hashes():
    collector = EventCollector()
    response = LLMResponse(kind="final", content="done")
    fake = FakeLLM([response])
    client = make_recording_client(fake, collector)
    messages = [
        Message(role="system", content="keep this first"),
        Message(
            role="assistant",
            content="calling tool",
            tool_calls=[ToolCall(call_id="call-1", name="lookup", arguments={"term": "Redis"})],
        ),
        Message(role="tool", content="result", tool_call_id="call-1"),
    ]
    tools = [
        ToolDefinition(
            name="lookup",
            description="search",
            parameters={"type": "object", "properties": {"term": {"type": "string"}}},
        )
    ]
    prompt = make_prompt_identity("rag.test", "1", "system: fixed template")

    with llm_question_scope("blog-dev-001"):
        with llm_prompt_scope("query_planner", prompt):
            actual = await client.complete(messages, tools)

    assert actual is response
    assert fake.calls[0][0] is messages
    assert fake.calls[0][1] is tools
    assert [message.role for message in fake.calls[0][0]] == ["system", "assistant", "tool"]
    request_type, request_event = collector.events[0]
    response_type, response_event = collector.events[1]
    assert request_type == "llm.request"
    assert response_type == "llm.response"
    request_payload = request_event["payload"]
    assert request_payload["purpose"] == "query_planner"
    assert request_payload["attempt"] == 1
    assert request_payload["messages"] == [message.model_dump(mode="json") for message in messages]
    assert request_payload["tools"] == [tool.model_dump(mode="json") for tool in tools]
    assert request_payload["messages_sha256"] == canonical_json_sha256(request_payload["messages"])
    assert request_payload["tools_sha256"] == canonical_json_sha256(request_payload["tools"])
    assert "api_key" not in repr(request_payload).lower()
    assert "authorization" not in repr(request_payload).lower()
    assert response_event["payload"]["response"] == {"kind": "final", "content": "done", "tool_call": None}


@pytest.mark.asyncio
async def test_recording_client_preserves_empty_tools_and_uses_independent_attempts():
    collector = EventCollector()
    failure = RuntimeError("provider request failed with private details")
    fake = FakeLLM(
        [
            LLMResponse(kind="final", content="one"),
            failure,
            LLMResponse(kind="final", content="three"),
        ]
    )
    client = make_recording_client(fake, collector)
    prompt = make_prompt_identity("rag.answer", "1", "answer template")

    with llm_question_scope("blog-dev-002"):
        with llm_prompt_scope("answerer", prompt):
            for index in range(3):
                messages = [Message(role="user", content=f"query-{index}")]
                tools: list[ToolDefinition] = []
                if index == 1:
                    with pytest.raises(RuntimeError) as caught:
                        await client.complete(messages, tools)
                    assert caught.value is failure
                else:
                    await client.complete(messages, tools)

    assert all(call_tools == [] for _, call_tools in fake.calls)
    assert [event[0] for event in collector.events] == [
        "llm.request",
        "llm.response",
        "llm.request",
        "llm.response",
        "llm.request",
        "llm.response",
    ]
    attempts = [
        collector.events[index][1]["payload"]["attempt"]
        for index in (0, 2, 4)
    ]
    assert attempts == [1, 2, 3]
    failed_payload = collector.events[3][1]["payload"]
    assert failed_payload["status"] == "failed"
    assert failed_payload["error_code"] == "client_error"
    assert "private details" not in repr(failed_payload)
    assert collector.events[3][1]["payload"]["attempt"] == 2


@pytest.mark.asyncio
async def test_recording_client_is_transparent_when_observer_is_not_configured():
    response = LLMResponse(kind="final", content="unchanged")
    fake = FakeLLM([response])
    client = RecordingLLMClient(fake)
    messages = [Message(role="user", content="hello")]
    tools: list[ToolDefinition] = []

    actual = await client.complete(messages, tools)

    assert actual is response
    assert fake.calls == [(messages, tools)]


@pytest.mark.asyncio
async def test_recording_client_hard_limit_blocks_call_before_forwarding():
    first = LLMResponse(kind="final", content="one")
    second = LLMResponse(kind="final", content="two")
    fake = FakeLLM([first, second])
    client = RecordingLLMClient(fake, max_calls=1)
    messages = [Message(role="user", content="hello")]

    assert await client.complete(messages, []) is first
    with pytest.raises(LLMObservationError, match="call limit"):
        await client.complete(messages, [])

    assert fake.calls == [(messages, [])]


def test_recording_client_rejects_non_positive_call_limit():
    fake = FakeLLM([])
    with pytest.raises(ValueError, match="max_calls"):
        RecordingLLMClient(fake, max_calls=0)


def test_prompt_identity_changes_when_template_source_changes():
    original = make_prompt_identity("rag.answer", "1", "fixed template")
    changed = make_prompt_identity("rag.answer", "1", "fixed template changed")

    assert original.prompt_id == changed.prompt_id
    assert original.version == changed.version
    assert original.sha256 != changed.sha256


@pytest.mark.asyncio
async def test_recording_client_requires_question_scope_when_observation_is_enabled():
    collector = EventCollector()
    fake = FakeLLM([LLMResponse(kind="final", content="unused")])
    client = make_recording_client(fake, collector)
    prompt = make_prompt_identity("rag.answer", "1", "answer template")

    with llm_prompt_scope("answerer", prompt):
        with pytest.raises(LLMObservationError, match="question scope"):
            await client.complete([Message(role="user", content="query")], [])

    assert fake.calls == []
    assert collector.events == []