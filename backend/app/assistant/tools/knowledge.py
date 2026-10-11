"""Reuse the knowledge service with the Run-scoped model client."""

import json

from pydantic import BaseModel, ConfigDict, Field

from app.agent.models import ToolDefinition
from app.assistant.models import AssistantResult, CapabilityError, CapabilityOutcome
from app.knowledge.service import CitationSnapshot
from app.tools.base import ToolResult
from app.tools.read_knowledge import ReadKnowledge, ReadKnowledgeInput
from .project import register_read_only


class KnowledgeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=500, pattern=r"\S")
    top_k: int = Field(default=5, ge=1, le=10)


class KnowledgeReadInput(ReadKnowledgeInput):
    model_config = ConfigDict(extra="forbid", strict=True)


class KnowledgeCapability:
    def __init__(self, name, service, *, mode="hybrid"):
        self.name = name
        self.service = service
        self.mode = mode
        self.input_model = KnowledgeReadInput if name == "read_knowledge" else KnowledgeInput

    def definition(self):
        return ToolDefinition(name=self.name, description={
            "search_knowledge": "Retrieve local evidence and citation snapshots; empty results mean insufficient material.",
            "read_knowledge": "Read a local knowledge chunk by its ID.",
            "answer_knowledge": "Generate an evidence-checked answer from local knowledge with shared model budget."
        }[self.name], parameters=self.input_model.model_json_schema())

    async def execute(self, arguments, context):
        data = self.input_model.model_validate(arguments)
        try:
            if self.name == "read_knowledge":
                result = await ReadKnowledge(self.service.repository).execute(arguments, context)
                if not result.success:
                    raise ValueError("knowledge_chunk_not_found")
                chunk = await self.service.repository.get_chunk(data.chunk_id)
                document = await self.service.repository.get_document(chunk.document_id)
                citation = CitationSnapshot(citation_id=chunk.chunk_id, chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id, document_version=chunk.document_version,
                    source_path=document.source_path, source_url=document.source_url, title=document.title,
                    heading_path=chunk.heading_path, start_line=chunk.start_line, end_line=chunk.end_line,
                    text=result.data["text"])
                answer = AssistantResult(answer=json.dumps(result.data, ensure_ascii=False), citations=[citation])
            elif self.name == "answer_knowledge":
                result = await self.service.answer(data.query, mode=self.mode, limit=data.top_k,
                                                   llm_client=context.model_client)
                answer = AssistantResult(answer=result.answer or "材料不足：现有证据无法生成有依据的回答。", citations=result.citations)
            else:
                result = await self.service.retrieve_for_assistant(data.query, mode=self.mode,
                    limit=data.top_k, llm_client=context.model_client)
                answer = AssistantResult(answer="Retrieved evidence" if result.citations else "材料不足：未检索到证据。",
                                         citations=result.citations)
            outcome = CapabilityOutcome(kind="result", result=answer)
            return ToolResult(success=True, data=outcome.model_dump(mode="json"))
        except ValueError as exc:
            # Domain errors are feedback, never a fabricated supported answer.
            code = "invalid_citation" if "citation" in str(exc) else "knowledge_request_failed"
            outcome = CapabilityOutcome(kind="failed", error=CapabilityError(category="recoverable_error", code=code))
            return ToolResult(success=False, data=outcome.model_dump(mode="json"))


def register_knowledge(registry, service, *, mode="hybrid"):
    for name in ("search_knowledge", "read_knowledge", "answer_knowledge"):
        register_read_only(registry, KnowledgeCapability(name, service, mode=mode))