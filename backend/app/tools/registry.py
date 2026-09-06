from typing import Any

from app.agent.models import ToolDefinition
from app.errors import ToolNotFoundError
from .base import ToolContext, ToolResult, run_with_timeout


class ToolRegistry:
    def __init__(self, tools: list[Any] | None = None):
        self._tools = {tool.name: tool for tool in (tools or [])}

    def register(self, tool: Any) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Any:
        if name not in self._tools:
            raise ToolNotFoundError(f"Unknown tool: {name}")
        return self._tools[name]

    def definitions(self) -> list[ToolDefinition]:
        return [tool.definition() for tool in self._tools.values()]

    async def execute(
        self, name: str, arguments: dict, context: ToolContext, timeout: float = 20
    ) -> ToolResult:
        return await run_with_timeout(self.get(name), arguments, context, timeout)
