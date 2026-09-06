from typing import Protocol

from pydantic import BaseModel, Field

from app.agent.models import ToolDefinition
from .base import ToolContext, ToolResult


class SearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=10)


class SearchProvider(Protocol):
    async def search(self, query: str, limit: int) -> list[dict]: ...


class MockSearchProvider:
    async def search(self, query: str, limit: int) -> list[dict]:
        return [
            {
                "title": f"Mock result for {query}",
                "url": "about:blank",
                "snippet": "Configure a real provider.",
            }
        ][:limit]


class Search:
    name = "search"
    description = "Search external information through a configured provider."
    input_model = SearchInput

    def __init__(self, provider: SearchProvider | None = None):
        self.provider = provider or MockSearchProvider()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=SearchInput.model_json_schema(),
        )

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        data = self.input_model.model_validate(arguments)
        return ToolResult(
            success=True,
            data={
                "query": data.query,
                "results": await self.provider.search(data.query, data.limit),
            },
        )
