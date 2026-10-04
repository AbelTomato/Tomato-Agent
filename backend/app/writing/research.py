from typing import Protocol

from app.knowledge.pipeline_models import RetrievalMode
from app.writing.citation_models import WritingCitation
from app.writing.execution_models import ExecutionConfig, ResearchBundle


class WritingCitationResult(Protocol):
    """Knowledge 返回引用在 Writing 边界所需的最小字段契约。"""

    citation_id: str
    chunk_id: str
    document_id: str
    document_version: str
    source_path: str
    source_url: str | None
    title: str
    heading_path: str
    start_line: int
    end_line: int
    text: str


class WritingResearchResult(Protocol):
    evidence_status: str
    retrieval_mode: RetrievalMode
    retrieval_fallback_reason: str | None
    citations: list[WritingCitationResult]


class WritingResearchQueryPort(Protocol):
    async def retrieve(
        self,
        query: str,
        *,
        mode: RetrievalMode,
        limit: int,
    ) -> WritingResearchResult:
        ...


class WritingResearcher:
    """Deterministic, single-pass research adapter for writing tasks."""

    def __init__(self, knowledge_service: WritingResearchQueryPort) -> None:
        self.knowledge_service = knowledge_service

    async def collect(self, topic: str, *, config: ExecutionConfig) -> ResearchBundle:
        answer = await self.knowledge_service.retrieve(
            topic,
            mode=config.retrieval_mode,
            limit=config.max_evidence,
        )
        writing_citations = [
            WritingCitation.model_validate(
                {
                    field: getattr(citation, field)
                    for field in WritingCitation.model_fields
                }
            )
            for citation in answer.citations
        ]
        return ResearchBundle(
            evidence_status=answer.evidence_status,
            citations=writing_citations,
            retrieval_mode=answer.retrieval_mode,
            retrieval_fallback_reason=answer.retrieval_fallback_reason,
        )
