from typing import Any

from pydantic import BaseModel, Field

from app.agent.models import ToolDefinition
from app.knowledge.repository import KnowledgeRepository

from .base import ToolContext, ToolResult


class SearchKnowledgeInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=10)


class SearchKnowledge:
    name = "search_knowledge"
    description = "Search the local knowledge base and return bounded evidence metadata."
    input_model = SearchKnowledgeInput

    def __init__(self, repository: KnowledgeRepository, max_result_chars: int = 20_000):
        if max_result_chars <= 0:
            raise ValueError("max_result_chars must be positive")
        self.repository = repository
        self.max_result_chars = max_result_chars

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=SearchKnowledgeInput.model_json_schema(),
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        data = self.input_model.model_validate(arguments)
        results = await self.repository.search_chunks(data.query, limit=data.top_k)
        return ToolResult(
            success=True,
            data={
                "query": data.query,
                "results": [
                    {
                        "chunk_id": result.chunk_id,
                        "document_id": result.document_id,
                        "document_version": result.document_version,
                        "source_path": result.source_path,
                        "source_url": result.source_url,
                        "title": result.title,
                        "heading_path": result.heading_path,
                        "start_line": result.start_line,
                        "end_line": result.end_line,
                        "text": result.text[: self.max_result_chars],
                        "score": result.score,
                    }
                    for result in results
                ],
            },
        )