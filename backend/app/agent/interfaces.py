from typing import Protocol

from .models import LLMResponse, Message, ToolDefinition


class LLMClient(Protocol):
    async def complete(
        self, messages: list[Message], tools: list[ToolDefinition]
    ) -> LLMResponse:
        ...