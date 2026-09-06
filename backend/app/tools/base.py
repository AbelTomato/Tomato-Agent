import asyncio
from typing import Any, Protocol

from pydantic import BaseModel

from app.agent.models import ToolDefinition


class ToolContext(BaseModel):
    session_id: str
    run_id: str


class ToolResult(BaseModel):
    success: bool
    data: Any = None
    error: str | None = None


class Tool(Protocol):
    name: str
    description: str
    input_model: type[BaseModel]

    def definition(self) -> ToolDefinition: ...

    async def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult: ...


async def run_with_timeout(
    tool: Tool, arguments: dict[str, Any], context: ToolContext, timeout: float
) -> ToolResult:
    try:
        return await asyncio.wait_for(tool.execute(arguments, context), timeout=timeout)
    except TimeoutError as exc:
        from app.errors import ToolTimeoutError

        raise ToolTimeoutError(f"Tool {tool.name} timed out") from exc
