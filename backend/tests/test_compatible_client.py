import json

import httpx
import pytest

from app.agent.models import Message, ToolCall, ToolDefinition
from app.llm.compatible_client import OpenAICompatibleClient


@pytest.mark.asyncio
async def test_complete_sends_openai_compatible_request_and_parses_final_response():
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["authorization"] = request.headers["authorization"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "done"}}
                ]
            },
        )

    client = OpenAICompatibleClient(
        api_key="test-key",
        model="test-model",
        base_url="https://provider.example/v1/",
        transport=httpx.MockTransport(handler),
    )
    result = await client.complete(
        [Message(role="system", content="Be concise.")],
        [
            ToolDefinition(
                name="calculator",
                description="Calculate",
                parameters={"type": "object"},
            )
        ],
    )

    assert result.kind == "final"
    assert result.content == "done"
    assert captured["path"] == "/v1/chat/completions"
    assert captured["authorization"] == "Bearer test-key"
    assert captured["payload"] == {
        "model": "test-model",
        "messages": [{"role": "system", "content": "Be concise."}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "calculator",
                    "description": "Calculate",
                    "parameters": {"type": "object"},
                },
            }
        ],
    }


@pytest.mark.asyncio
async def test_complete_converts_tool_call_arguments_to_dict():
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["messages"] == [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "calculator",
                            "arguments": '{"expression": "1 + 4"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "content": '{"value": 5}',
                "tool_call_id": "call-1",
            },
        ]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-2",
                                    "type": "function",
                                    "function": {
                                        "name": "calculator",
                                        "arguments": '{"expression":"2 + 3"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    client = OpenAICompatibleClient(
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    result = await client.complete(
        [
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        call_id="call-1",
                        name="calculator",
                        arguments={"expression": "1 + 4"},
                    )
                ],
            ),
            Message(
                role="tool",
                content='{"value": 5}',
                tool_call_id="call-1",
            ),
        ],
        [],
    )

    assert result.kind == "tool_call"
    assert result.tool_call is not None
    assert result.tool_call.call_id == "call-2"
    assert result.tool_call.name == "calculator"
    assert result.tool_call.arguments == {"expression": "2 + 3"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"choices": []}, "does not contain choices"),
        ({"choices": [{"message": {"content": ""}}]}, "empty content"),
        (
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {
                                        "name": "calculator",
                                        "arguments": "not-json",
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            "invalid tool arguments",
        ),
    ],
)
async def test_complete_rejects_invalid_provider_responses(response, message):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    client = OpenAICompatibleClient(
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError, match=message):
        await client.complete([], [])


@pytest.mark.asyncio
async def test_complete_rejects_http_error_without_exposing_api_key():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="secret provider details")

    client = OpenAICompatibleClient(
        api_key="super-secret-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError) as error:
        await client.complete([], [])

    assert str(error.value) == "LLM provider request failed: HTTP 401"
    assert "super-secret-key" not in str(error.value)


def test_client_validates_model_and_timeout():
    with pytest.raises(ValueError, match="model cannot be empty"):
        OpenAICompatibleClient(api_key="key", model=" ")
    with pytest.raises(ValueError, match="timeout_seconds"):
        OpenAICompatibleClient(api_key="key", model="model", timeout_seconds=0)


def test_tool_message_requires_tool_call_id():
    client = OpenAICompatibleClient(api_key="key", model="model")

    with pytest.raises(ValueError, match="tool_call_id"):
        client._message_to_provider(Message(role="tool", content="result"))