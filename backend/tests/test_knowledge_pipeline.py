import pytest

from app.knowledge.answerability import AnswerabilityConfig, CoverageAnswerabilityJudge
from app.knowledge.evidence_selection import CoverageAwareEvidenceSelector
from app.knowledge.models import SearchResult
from app.knowledge.pipeline_models import CandidateEvidence
from app.knowledge.pipeline_models import QueryPlan, RetrievalQuery
from app.knowledge.query_planning import SafeQueryPlanner
from app.knowledge.reranking import NoopReranker, RerankerError
from app.knowledge.service import KnowledgePipeline


def evidence(chunk_id: str, document_id: str, query_ids: tuple[str, ...]) -> CandidateEvidence:
    return CandidateEvidence(
        result=SearchResult(
            chunk_id=chunk_id,
            document_id=document_id,
            document_version="v1",
            source_path=f"{document_id}.md",
            source_url=None,
            title=document_id,
            heading_path="正文",
            start_line=1,
            end_line=2,
            text=f"证据 {chunk_id}",
            score=0.5,
        ),
        query_ids=query_ids,
        retrieval_ranks=tuple(range(1, len(query_ids) + 1)),
        retrieval_score_type="bm25",
    )


class FakeCandidateRetriever:
    def __init__(self, candidates):
        self.candidates = tuple(candidates)
        self.calls = []

    async def retrieve(self, plan, *, mode, candidate_limit):
        self.calls.append((plan, mode, candidate_limit))
        return self.candidates


class FailingReranker:
    async def rank(self, query, candidates):
        raise RerankerError("provider failed")


class TwoQueryPlanner:
    async def plan(self, query, *, llm_client=None):
        return QueryPlan(
            original_query=query,
            queries=(
                RetrievalQuery(query_id="q1", text="证据一", facet="一"),
                RetrievalQuery(query_id="q2", text="证据二", facet="二"),
            ),
            is_multi_evidence=True,
        )


@pytest.mark.asyncio
async def test_pipeline_keeps_two_cross_document_evidence_and_separates_limits():
    retriever = FakeCandidateRetriever(
        [evidence("q1", "doc-a", ("q1",)), evidence("q2", "doc-b", ("q2",))]
    )
    pipeline = KnowledgePipeline(
        planner=TwoQueryPlanner(),
        candidate_retriever=retriever,
        reranker=NoopReranker(),
        selector=CoverageAwareEvidenceSelector(),
        judge=CoverageAnswerabilityJudge(),
        answerability_config=AnswerabilityConfig(),
    )

    result = await pipeline.run("Q/K/V 以及多头注意力", mode="keyword", candidate_limit=30, final_limit=5)

    assert len(result.candidates) == 2
    assert len(result.selection.selected) == 2
    assert result.decision.status == "supported"
    assert retriever.calls[0][2] == 30
    assert result.selection.final_limit == 5


@pytest.mark.asyncio
async def test_pipeline_preserves_insufficient_status_without_treating_candidates_as_supported():
    retriever = FakeCandidateRetriever([evidence("q1", "doc-a", ("q1",))])
    pipeline = KnowledgePipeline(
        planner=TwoQueryPlanner(),
        candidate_retriever=retriever,
        reranker=NoopReranker(),
        selector=CoverageAwareEvidenceSelector(),
        judge=CoverageAnswerabilityJudge(),
        answerability_config=AnswerabilityConfig(),
    )

    result = await pipeline.run("两个证据面", mode="keyword", candidate_limit=30, final_limit=5)

    assert result.candidates
    assert result.decision.status == "insufficient"
    assert result.decision.reason == "missing_required_query"


@pytest.mark.asyncio
async def test_pipeline_propagates_rerank_error_instead_of_returning_empty_result():
    pipeline = KnowledgePipeline(
        planner=SafeQueryPlanner(),
        candidate_retriever=FakeCandidateRetriever([evidence("q1", "doc", ("q1",))]),
        reranker=FailingReranker(),
        selector=CoverageAwareEvidenceSelector(),
        judge=CoverageAnswerabilityJudge(),
        answerability_config=AnswerabilityConfig(),
    )

    with pytest.raises(RerankerError, match="provider failed"):
        await pipeline.run("问题", mode="keyword", candidate_limit=30, final_limit=5)