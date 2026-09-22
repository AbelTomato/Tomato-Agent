from typing import Any

from pydantic import BaseModel, Field

from app.agent.models import ToolDefinition
from app.knowledge.repository import KnowledgeRepository

from .base import ToolContext, ToolResult


class ReadKnowledgeInput(BaseModel):
    chunk_id: str = Field(min_length=1, max_length=200)


class ReadKnowledge:
    name = "read_knowledge"
    description = "Read one bounded local knowledge chunk by its opaque chunk ID."
    input_model = ReadKnowledgeInput

    def __init__(self, repository: KnowledgeRepository, max_result_chars: int = 20_000):
        if max_result_chars <= 0:
            raise ValueError("max_result_chars must be positive")
        self.repository = repository
        self.max_result_chars = max_result_chars

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=ReadKnowledgeInput.model_json_schema(),
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        data = self.input_model.model_validate(arguments)
        chunk = await self.repository.get_chunk(data.chunk_id)
        if chunk is None:
            return ToolResult(success=False, error="Knowledge chunk not found")
        context.read_chunk_ids.add(chunk.chunk_id)
        context.read_document_versions.add(chunk.document_version)
        return ToolResult(
            success=True,
            data={
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "document_version": chunk.document_version,
                "heading_path": chunk.heading_path,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "text": chunk.text[: self.max_result_chars],
            },
        )