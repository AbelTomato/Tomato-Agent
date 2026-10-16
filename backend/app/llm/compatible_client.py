import json
import contextvars
from typing import Any, Callable

import httpx

from app.agent.interfaces import LLMClient
from app.agent.models import LLMResponse, Message, ToolCall, ToolDefinition

_REQUEST_CONTEXT = contextvars.ContextVar("provider_request_context", default=None)


class OpenAICompatibleClient(LLMClient):
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        request_observer: Callable[[dict[str, Any], httpx.Response | None], None] | None = None,
        stream: bool = False,
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
        self.request_observer = request_observer
        self.stream = stream
        self.last_token_usage: dict[str, int] | None = None

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        response_format: dict[str, Any] | None = None,
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
            # Runtime executes exactly one tool call per LLM turn.
            payload["tool_choice"] = "auto"
            payload["parallel_tool_calls"] = False

        if response_format is not None:
            payload["response_format"] = response_format
        if self.stream:
            payload["stream"] = True

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(
            timeout=self.timeout,
            transport=self.transport,
        ) as client:
            try:
                if self.stream:
                    response, data = await self._stream_completion(client, headers, payload)
                else:
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    )
                    data = None
            except BaseException:
                observer = self.request_observer or _REQUEST_CONTEXT.get()
                if observer is not None:
                    observer(payload, None)
                raise

        observer = self.request_observer or _REQUEST_CONTEXT.get()
        if observer is not None:
            observer(payload, response)

        if response.is_error:
            raise RuntimeError(
                f"LLM provider request failed: HTTP {response.status_code}"
            )

        if data is None:
            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError("LLM provider returned invalid JSON") from exc

        if not isinstance(data, dict):
            raise RuntimeError("LLM provider returned an invalid response object")

        usage = data.get("usage")
        self.last_token_usage = (
            {
                key: value
                for key, value in usage.items()
                if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                and isinstance(value, int)
            }
            if isinstance(usage, dict)
            else None
        )
        return self._response_from_provider(data)

    async def _stream_completion(self, client, headers, payload):
        chunks: list[str] = []
        raw_events: list[str] = []
        content: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason = None
        usage = None

        async with client.stream(
            "POST", f"{self.base_url}/chat/completions", headers=headers, json=payload
        ) as streamed:
            streamed.raise_for_status()
            async for line in streamed.aiter_lines():
                if not line.startswith("data:"):
                    continue
                event = line[5:].strip()
                raw_events.append(f"data: {event}")
                if event == "[DONE]":
                    continue
                try:
                    item = json.loads(event)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("LLM provider returned invalid streaming JSON") from exc
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("usage"), dict):
                    usage = item["usage"]
                choices = item.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, dict):
                    continue
                finish_reason = choice.get("finish_reason") or finish_reason
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    continue
                text = delta.get("content")
                if isinstance(text, str):
                    content.append(text)
                fragments = delta.get("tool_calls")
                if isinstance(fragments, list):
                    for fragment in fragments:
                        if not isinstance(fragment, dict):
                            continue
                        index = fragment.get("index", 0)
                        if not isinstance(index, int):
                            continue
                        target = tool_calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        if isinstance(fragment.get("id"), str):
                            target["id"] += fragment["id"]
                        function = fragment.get("function")
                        if isinstance(function, dict):
                            for key in ("name", "arguments"):
                                value = function.get(key)
                                if isinstance(value, str):
                                    target["function"][key] += value
            response = httpx.Response(
                streamed.status_code,
                headers=streamed.headers,
                content="\n".join(raw_events).encode("utf-8"),
                request=streamed.request,
            )
        data: dict[str, Any] = {"choices": [{"finish_reason": finish_reason}]}
        if tool_calls:
            data["choices"][0]["message"] = {
                "tool_calls": [tool_calls[index] for index in sorted(tool_calls)]
            }
        else:
            data["choices"][0]["message"] = {"content": "".join(content)}
        if usage is not None:
            data["usage"] = usage
        return response, data

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

            if len(provider_tool_calls) != 1:
                raise RuntimeError(
                    "LLM provider returned multiple tool calls; "
                    "parallel tool execution is not supported"
                )

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
