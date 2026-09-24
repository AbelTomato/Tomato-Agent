import pytest

from app.knowledge.answerability import AnswerabilityConfig, CoverageAnswerabilityJudge
from app.knowledge.evidence_selection import CoverageAwareEvidenceSelector
from app.knowledge.models import SearchResult
from app.knowledge.pipeline_models import CandidateEvidence, EvidenceSelection
from app.knowledge.pipeline_models import QueryPlan, RetrievalQuery
from app.knowledge.query_planning import SafeQueryPlanner
from app.knowledge.reranking import NoopReranker, RerankerError
from app.knowledge.service import KnowledgePipeline


def evidence(
    chunk_id: str,
    document_id: str,
    query_ids: tuple[str, ...],
    *,
    retrieval_ranks: tuple[int, ...] | None = None,
    retrieval_scores: tuple[float, ...] = (),
) -> CandidateEvidence:
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
        retrieval_ranks=(
            retrieval_ranks
            if retrieval_ranks is not None
            else tuple(range(1, len(query_ids) + 1))
        ),
        retrieval_scores=retrieval_scores,
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


class TraceCollector:
    def __init__(self):
        self.events = []

    def emit(self, event_type, *, status, payload, duration_ms):
        self.events.append(
            {
                "event_type": event_type,
                "status": status,
                "payload": payload,
                "duration_ms": duration_ms,
            }
        )


class FailingTraceCollector(TraceCollector):
    def emit(self, event_type, *, status, payload, duration_ms):
        if status == "failed":
            raise RuntimeError("observer failed")
        super().emit(
            event_type,
            status=status,
            payload=payload,
            duration_ms=duration_ms,
        )


class ReorderingReranker:
    async def rank(self, query, candidates):
        ranked = []
        for index, candidate in enumerate(candidates):
            chunk_id = candidate.result.chunk_id
            score = {
                "q1-span": 0.8,
                "q2-span": 0.99,
                "filler-0": 0.2,
                "filler-1": 0.3,
                "filler-2": 0.4,
                "filler-3": 0.5,
                "filler-4": 0.6,
            }.get(chunk_id, 0.8 - index * 0.1)
            ranked.append(candidate.model_copy(update={"rerank_score": score}))
        return sorted(ranked, key=lambda item: item.rerank_score, reverse=True)


class CustomSelectorWithoutDispositions:
    def select(self, plan, candidates, *, final_limit):
        selected = tuple(candidates[:final_limit])
        covered_query_ids = tuple(
            query.query_id
            for query in plan.queries
            if any(query.query_id in candidate.query_ids for candidate in selected)
        )
        return EvidenceSelection(
            selected=selected,
            covered_query_ids=covered_query_ids,
            covered_document_ids=tuple(
                dict.fromkeys(candidate.result.document_id for candidate in selected)
            ),
            final_limit=final_limit,
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
async def test_pipeline_keeps_blog_formal_014_second_span_inside_final_limit():
    filler = [
        evidence(
            f"filler-{index}",
            "doc-z-filler",
            ("q1", "q2") if index == 0 else ("q1",),
        )
        for index in range(5)
    ]
    retriever = FakeCandidateRetriever(
        [
            evidence("q1-span", "doc-q1", ("q1",)),
            *filler,
            evidence("q2-span", "doc-q2", ("q2",)),
        ]
    )
    pipeline = KnowledgePipeline(
        planner=TwoQueryPlanner(),
        candidate_retriever=retriever,
        reranker=NoopReranker(),
        selector=CoverageAwareEvidenceSelector(),
        judge=CoverageAnswerabilityJudge(),
        answerability_config=AnswerabilityConfig(),
    )

    result = await pipeline.run(
        "Q、K、V 的职责如何帮助理解多头注意力？",
        mode="keyword",
        candidate_limit=30,
        final_limit=5,
    )

    assert [item.result.chunk_id for item in result.selection.selected] == [
        "q1-span",
        "q2-span",
        "filler-0",
        "filler-1",
        "filler-2",
    ]
    assert result.decision.status == "supported"


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
    assert result.decision.reason == "partial_coverage"


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


@pytest.mark.asyncio
async def test_pipeline_observer_records_traceable_stage_payloads_without_changing_result():
    collector = TraceCollector()
    filler = [
        evidence(
            "filler-0",
            "doc-z-filler",
            ("q1", "q2"),
            retrieval_ranks=(1, 2),
            retrieval_scores=(0.5, 0.4),
        ),
        *[
            evidence(f"filler-{index}", "doc-z-filler", ("q1",))
            for index in range(1, 5)
        ],
    ]
    pipeline = KnowledgePipeline(
        planner=TwoQueryPlanner(),
        candidate_retriever=FakeCandidateRetriever(
            [
                evidence("q1-span", "doc-q1", ("q1",)),
                *filler,
                evidence("q2-span", "doc-q2", ("q2",)),
            ]
        ),
        reranker=ReorderingReranker(),
        selector=CoverageAwareEvidenceSelector(),
        judge=CoverageAnswerabilityJudge(),
        answerability_config=AnswerabilityConfig(),
        observer=collector,
    )

    result = await pipeline.run("问题", mode="keyword", candidate_limit=30, final_limit=5)

    assert result.decision.status == "supported"
    assert [event["event_type"] for event in collector.events] == [
        "query_plan.completed",
        "retrieval.completed",
        "rerank.completed",
        "evidence_selection.completed",
        "answerability.completed",
    ]
    assert all(event["status"] == "success" for event in collector.events)
    assert all(event["duration_ms"] >= 0 for event in collector.events)

    plan_payload = collector.events[0]["payload"]
    assert [item["query_id"] for item in plan_payload["queries"]] == ["q1", "q2"]

    retrieval_payload = collector.events[1]["payload"]
    assert retrieval_payload["per_query_results"][0] == {
        "query_id": "q1",
        "results": [
            {"merged_candidate_index": 1, "chunk_id": "q1-span", "retrieval_rank": 1},
            {"merged_candidate_index": 2, "chunk_id": "filler-0", "retrieval_rank": 1},
            {"merged_candidate_index": 3, "chunk_id": "filler-1", "retrieval_rank": 1},
            {"merged_candidate_index": 4, "chunk_id": "filler-2", "retrieval_rank": 1},
            {"merged_candidate_index": 5, "chunk_id": "filler-3", "retrieval_rank": 1},
            {"merged_candidate_index": 6, "chunk_id": "filler-4", "retrieval_rank": 1},
        ],
    }
    assert retrieval_payload["merged_candidates"][0]["query_ids"] == ["q1"]
    assert retrieval_payload["merged_candidates"][0]["result"]["chunk_id"] == "q1-span"
    assert retrieval_payload["merged_candidates"][0]["query_retrievals"] == [
        {
            "query_id": "q1",
            "retrieval_rank": 1,
            "retrieval_score": 0.5,
            "score_type": "bm25",
        }
    ]
    shared_query_retrievals = retrieval_payload["merged_candidates"][1]["query_retrievals"]
    assert shared_query_retrievals == [
        {
            "query_id": "q1",
            "retrieval_rank": 1,
            "retrieval_score": 0.5,
            "score_type": "bm25",
        },
        {
            "query_id": "q2",
            "retrieval_rank": 2,
            "retrieval_score": 0.4,
            "score_type": "bm25",
        },
    ]
    assert retrieval_payload["per_query_results"][1]["results"] == [
        {"merged_candidate_index": 2, "chunk_id": "filler-0", "retrieval_rank": 2},
        {"merged_candidate_index": 7, "chunk_id": "q2-span", "retrieval_rank": 1},
    ]

    rerank_payload = collector.events[2]["payload"]
    assert rerank_payload["before"][0]["result"]["chunk_id"] == "q1-span"
    assert rerank_payload["before"][0]["rerank_score"] is None
    assert rerank_payload["after"][0]["result"]["chunk_id"] == "q2-span"
    assert rerank_payload["after"][0]["rerank_score"] == 0.99

    selection_payload = collector.events[3]["payload"]
    selected = {
        item["result"]["chunk_id"]: item
        for item in selection_payload["candidates"]
        if item["selected"]
    }
    selected_by_order = [
        chunk_id
        for chunk_id, _ in sorted(
            selected.items(), key=lambda item: item[1]["selected_order"]
        )
    ]
    assert selected_by_order == ["q2-span", "q1-span", "filler-4", "filler-3", "filler-2"]
    assert any(
        item["excluded_reason"] == "final_limit"
        for item in selection_payload["candidates"]
        if not item["selected"]
    )
    assert selection_payload["selected_chunk_ids"] == selected_by_order
    second_query_span = next(
        item
        for item in selection_payload["candidates"]
        if item["result"]["chunk_id"] == "q2-span"
    )
    assert second_query_span["selected"] is True
    assert second_query_span["selected_order"] == 1

    assert collector.events[4]["payload"]["decision"]["status"] == "supported"


@pytest.mark.asyncio
async def test_pipeline_observer_records_stage_failure_and_reraises_original_error():
    collector = TraceCollector()
    pipeline = KnowledgePipeline(
        planner=SafeQueryPlanner(),
        candidate_retriever=FakeCandidateRetriever([evidence("q1", "doc", ("q1",))]),
        reranker=FailingReranker(),
        selector=CoverageAwareEvidenceSelector(),
        judge=CoverageAnswerabilityJudge(),
        answerability_config=AnswerabilityConfig(),
        observer=collector,
    )

    with pytest.raises(RerankerError, match="provider failed"):
        await pipeline.run("问题", mode="keyword", candidate_limit=30, final_limit=5)

    assert [event["event_type"] for event in collector.events] == [
        "query_plan.completed",
        "retrieval.completed",
        "rerank.failed",
    ]
    assert collector.events[-1]["status"] == "failed"
    assert collector.events[-1]["payload"] == {"error_code": "reranker_error"}


@pytest.mark.asyncio
async def test_pipeline_failure_preserves_original_error_if_observer_also_fails():
    pipeline = KnowledgePipeline(
        planner=SafeQueryPlanner(),
        candidate_retriever=FakeCandidateRetriever([evidence("q1", "doc", ("q1",))]),
        reranker=FailingReranker(),
        selector=CoverageAwareEvidenceSelector(),
        judge=CoverageAnswerabilityJudge(),
        answerability_config=AnswerabilityConfig(),
        observer=FailingTraceCollector(),
    )

    with pytest.raises(RerankerError, match="provider failed"):
        await pipeline.run("问题", mode="keyword", candidate_limit=30, final_limit=5)


@pytest.mark.asyncio
async def test_pipeline_without_observer_preserves_existing_result_and_does_not_emit():
    pipeline = KnowledgePipeline(
        planner=SafeQueryPlanner(),
        candidate_retriever=FakeCandidateRetriever([evidence("q1", "doc", ("q1",))]),
        reranker=NoopReranker(),
        selector=CoverageAwareEvidenceSelector(),
        judge=CoverageAnswerabilityJudge(),
        answerability_config=AnswerabilityConfig(),
    )

    result = await pipeline.run("问题", mode="keyword", candidate_limit=30, final_limit=5)

    assert result.decision.status == "supported"


@pytest.mark.asyncio
async def test_pipeline_observer_uses_generic_disposition_for_custom_selector():
    collector = TraceCollector()
    pipeline = KnowledgePipeline(
        planner=SafeQueryPlanner(),
        candidate_retriever=FakeCandidateRetriever(
            [
                evidence("selected", "doc-a", ("q1",)),
                evidence("omitted", "doc-b", ("q1",)),
            ]
        ),
        reranker=NoopReranker(),
        selector=CustomSelectorWithoutDispositions(),
        judge=CoverageAnswerabilityJudge(),
        answerability_config=AnswerabilityConfig(),
        observer=collector,
    )

    await pipeline.run("问题", mode="keyword", candidate_limit=30, final_limit=1)

    candidates = collector.events[3]["payload"]["candidates"]
    assert [(item["selected"], item["excluded_reason"]) for item in candidates] == [
        (True, None),
        (False, "not_selected_by_selector"),
    ]