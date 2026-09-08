import json
from typing import Any

import httpx

from app.agent.interfaces import LLMClient
from app.agent.models import LLMResponse, Message, ToolCall, ToolDefinition


class OpenAICompatibleClient(LLMClient):
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model cannot be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds)
        self.transport = transport

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [self._message_to_provider(item) for item in messages],
        }

        if tools:
            payload["tools"] = [
                self._tool_to_provider(item)
                for item in tools
            ]

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(
            timeout=self.timeout,
            transport=self.transport,
        ) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )

        if response.is_error:
            raise RuntimeError(
                f"LLM provider request failed: HTTP {response.status_code}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("LLM provider returned invalid JSON") from exc

        if not isinstance(data, dict):
            raise RuntimeError("LLM provider returned an invalid response object")

        return self._response_from_provider(data)

    def _message_to_provider(self, message: Message) -> dict[str, Any]:
        result: dict[str, Any] = {
            "role": message.role,
            "content": message.content,
        }

        if message.role == "assistant" and message.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tool_call.call_id,
                    "type": "function",
                    "function": {
                        "name": tool_call.name,
                        "arguments": json.dumps(
                            tool_call.arguments,
                            ensure_ascii=False,
                        ),
                    },
                }
                for tool_call in message.tool_calls
            ]

        if message.role == "tool":
            if not message.tool_call_id:
                raise ValueError(
                    "Tool message requires tool_call_id"
                )
            result["tool_call_id"] = message.tool_call_id

        return result

    def _tool_to_provider(
        self,
        tool: ToolDefinition,
    ) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }

    def _response_from_provider(
        self,
        data: dict[str, Any],
    ) -> LLMResponse:
        choices = data.get("choices")

        if not isinstance(choices, list) or not choices:
            raise RuntimeError(
                "LLM provider response does not contain choices"
            )

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise RuntimeError(
                "LLM provider returned an invalid choice"
            )

        provider_message = first_choice.get("message")
        if not isinstance(provider_message, dict):
            raise RuntimeError(
                "LLM provider response does not contain a message"
            )

        provider_tool_calls = provider_message.get("tool_calls")

        if provider_tool_calls:
            if not isinstance(provider_tool_calls, list):
                raise RuntimeError(
                    "LLM provider returned invalid tool_calls"
                )

            # 当前 LLMResponse 只能承载一个 ToolCall。
            provider_tool_call = provider_tool_calls[0]
            return LLMResponse(
                kind="tool_call",
                tool_call=self._tool_call_from_provider(
                    provider_tool_call
                ),
            )

        content = provider_message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError(
                "LLM provider returned an empty content response"
            )

        return LLMResponse(
            kind="final",
            content=content,
        )

    def _tool_call_from_provider(
        self,
        raw_tool_call: Any,
    ) -> ToolCall:
        if not isinstance(raw_tool_call, dict):
            raise RuntimeError(
                "LLM provider returned an invalid tool call"
            )

        call_id = raw_tool_call.get("id")
        function = raw_tool_call.get("function")

        if not isinstance(call_id, str) or not call_id:
            raise RuntimeError(
                "LLM provider tool call is missing id"
            )

        if not isinstance(function, dict):
            raise RuntimeError(
                "LLM provider tool call is missing function"
            )

        name = function.get("name")
        raw_arguments = function.get("arguments", "{}")

        if not isinstance(name, str) or not name:
            raise RuntimeError(
                "LLM provider tool call is missing function name"
            )

        if not isinstance(raw_arguments, str):
            raise RuntimeError(
                "LLM provider tool call arguments must be a JSON string"
            )

        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "LLM provider returned invalid tool arguments"
            ) from exc

        if not isinstance(arguments, dict):
            raise RuntimeError(
                "LLM tool arguments must be a JSON object"
            )

        return ToolCall(
            call_id=call_id,
            name=name,
            arguments=arguments,
        )
