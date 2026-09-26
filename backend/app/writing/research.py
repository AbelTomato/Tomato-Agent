from app.knowledge.service import KnowledgeService
from app.writing.execution_models import ExecutionConfig, ResearchBundle


class WritingResearcher:
    """Deterministic, single-pass research adapter for writing tasks."""

    def __init__(self, knowledge_service: KnowledgeService) -> None:
        self.knowledge_service = knowledge_service

    async def collect(self, topic: str, *, config: ExecutionConfig) -> ResearchBundle:
        answer = await self.knowledge_service.retrieve(
            topic,
            mode=config.retrieval_mode,
            limit=config.max_evidence,
        )
        return ResearchBundle(
            evidence_status=answer.evidence_status,
            citations=[citation.model_copy(deep=True) for citation in answer.citations],
            retrieval_mode=config.retrieval_mode,
        )