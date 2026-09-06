from typing import Protocol

from .models import LLMResponse, Message, ToolDefinition


class LLMClient(Protocol):
    async def complete(
        self, messages: list[Message], tools: list[ToolDefinition]
    ) -> LLMResponse:
        """由使用者实现真实 LLM API 调用。"""
