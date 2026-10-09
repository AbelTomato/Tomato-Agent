from typing import Any

from app.agent.models import ToolDefinition
from app.agent.policies import ToolDeclaration
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

    def declarations(self) -> tuple[ToolDeclaration, ...]:
        """Expose policy metadata without changing the legacy definitions API."""

        declarations = []
        for tool in self._tools.values():
            if callable(getattr(tool, "declaration", None)):
                declarations.append(tool.declaration())
                continue
            definition = tool.definition()
            declarations.append(
                ToolDeclaration(
                    name=definition.name,
                    description=definition.description,
                    input_schema=definition.parameters,
                    capabilities=frozenset({"resource"}),
                    side_effect="read",
                )
            )
        return tuple(declarations)

    async def execute(
        self, name: str, arguments: dict, context: ToolContext, timeout: float = 20
    ) -> ToolResult:
        return await run_with_timeout(self.get(name), arguments, context, timeout)
