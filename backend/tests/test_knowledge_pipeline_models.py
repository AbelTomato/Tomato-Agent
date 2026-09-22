from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from app.knowledge.models import SearchResult
from app.knowledge.pipeline_models import (
    AnswerabilityDecision,
    CandidateEvidence,
    EvidenceSelection,
    PipelineResult,
    QueryPlan,
    RetrievalQuery,
)


def make_result(
    *,
    chunk_id: str = "chunk-1",
    document_id: str = "document-1",
    score: float = 0.8,
    start_line: int = 10,
    end_line: int = 14,
) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        document_id=document_id,
        document_version="version-1",
        source_path=f"{document_id}.md",
        source_url=None,
        title="测试文档",
        heading_path="章节",
        start_line=start_line,
        end_line=end_line,
        text="检索证据正文",
        score=score,
    )


def make_query(query_id: str = "q1", text: str = "原始问题", facet: str = "original") -> RetrievalQuery:
    return RetrievalQuery(query_id=query_id, text=text, facet=facet)


def make_candidate(
    *,
    result: SearchResult | None = None,
    query_ids: tuple[str, ...] = ("q1",),
    retrieval_ranks: tuple[int, ...] = (1,),
    retrieval_score_type: str = "cosine",
    rerank_score: float | None = None,
) -> CandidateEvidence:
    return CandidateEvidence(
        result=result or make_result(),
        query_ids=query_ids,
        retrieval_ranks=retrieval_ranks,
        retrieval_score_type=retrieval_score_type,
        rerank_score=rerank_score,
    )


def test_query_plan_preserves_original_query_and_is_immutable():
    plan = QueryPlan(
        original_query="原始问题",
        queries=(make_query(), make_query("q2", "第二个证据面", "facet-2")),
        is_multi_evidence=True,
    )

    assert plan.original_query == "原始问题"
    assert plan.queries[0].query_id == "q1"
    assert plan.is_multi_evidence is True
    with pytest.raises(ValidationError):
        plan.original_query = "被修改的问题"
    with pytest.raises(ValidationError):
        plan.queries += (make_query("q3"),)


@pytest.mark.parametrize(
    "value",
    [
        {"original_query": "", "queries": (make_query(),), "is_multi_evidence": False},
        {"original_query": "   ", "queries": (make_query(),), "is_multi_evidence": False},
        {"original_query": "问题", "queries": (), "is_multi_evidence": False},
        {
            "original_query": "问题",
            "queries": (make_query("same"), make_query("same")),
            "is_multi_evidence": True,
        },
    ],
)
def test_query_plan_rejects_empty_or_duplicate_queries(value):
    with pytest.raises(ValidationError):
        QueryPlan(**value)


@pytest.mark.parametrize(
    "value",
    [
        {"query_id": "", "text": "问题", "facet": "original"},
        {"query_id": "q1", "text": "", "facet": "original"},
        {"query_id": "q1", "text": "问题", "facet": ""},
    ],
)
def test_retrieval_query_rejects_empty_fields(value):
    with pytest.raises(ValidationError):
        RetrievalQuery(**value)


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf")])
def test_candidate_evidence_rejects_non_finite_scores(score: float):
    with pytest.raises(ValidationError):
        make_candidate(result=make_result(score=score))
    with pytest.raises(ValidationError):
        make_candidate(rerank_score=score)


def test_candidate_evidence_rejects_invalid_line_range_and_mismatched_ranks():
    with pytest.raises(ValidationError):
        make_candidate(result=make_result(start_line=20, end_line=19))
    with pytest.raises(ValidationError):
        make_candidate(query_ids=("q1", "q2"), retrieval_ranks=(1,))
    with pytest.raises(ValidationError):
        make_candidate(retrieval_ranks=(0,))


def test_candidate_evidence_rejects_empty_identity_and_keeps_stage_scores_separate():
    with pytest.raises(ValidationError):
        make_candidate(query_ids=())
    with pytest.raises(ValidationError):
        make_candidate(retrieval_score_type="")

    candidate = make_candidate(rerank_score=0.95)
    assert candidate.result.score == 0.8
    assert candidate.rerank_score == 0.95
    with pytest.raises(ValidationError):
        candidate.rerank_score = 0.5


def test_selection_can_express_insufficient_multi_evidence_without_mutation():
    first = make_candidate()
    selection = EvidenceSelection(
        selected=(first,),
        covered_query_ids=("q1",),
        covered_document_ids=("document-1",),
        final_limit=5,
    )
    decision = AnswerabilityDecision(
        status="insufficient",
        reason="missing_required_query",
        coverage_ratio=0.5,
        confidence=None,
    )

    assert selection.selected == (first,)
    assert selection.final_limit == 5
    assert decision.status == "insufficient"
    assert decision.coverage_ratio == 0.5
    with pytest.raises(ValidationError):
        selection.covered_query_ids += ("q2",)


@pytest.mark.parametrize("coverage_ratio", [-0.01, 1.01, float("nan"), float("inf")])
def test_answerability_decision_rejects_invalid_coverage(coverage_ratio: float):
    with pytest.raises(ValidationError):
        AnswerabilityDecision(
            status="supported",
            reason="full_query_coverage",
            coverage_ratio=coverage_ratio,
            confidence=0.9,
        )


def test_answerability_decision_rejects_invalid_status_and_confidence():
    with pytest.raises(ValidationError):
        AnswerabilityDecision(
            status="unknown",
            reason="bad_status",
            coverage_ratio=0.0,
            confidence=None,
        )
    with pytest.raises(ValidationError):
        AnswerabilityDecision(
            status="supported",
            reason="full_query_coverage",
            coverage_ratio=1.0,
            confidence=-0.1,
        )


def test_pipeline_result_contains_traceable_stage_results_and_non_negative_latencies():
    query = make_query()
    plan = QueryPlan(original_query="问题", queries=(query,), is_multi_evidence=False)
    candidate = make_candidate()
    selection = EvidenceSelection(
        selected=(candidate,),
        covered_query_ids=("q1",),
        covered_document_ids=("document-1",),
        final_limit=1,
    )
    decision = AnswerabilityDecision(
        status="supported",
        reason="full_query_coverage",
        coverage_ratio=1.0,
        confidence=0.9,
    )
    result = PipelineResult(
        plan=plan,
        candidates=(candidate,),
        selection=selection,
        decision=decision,
        candidate_latency_ms=1.0,
        rerank_latency_ms=2.0,
        selection_latency_ms=3.0,
        judge_latency_ms=4.0,
    )

    assert result.plan == plan
    assert result.candidates == (candidate,)
    assert result.decision.status == "supported"
    with pytest.raises(ValidationError):
        result.candidate_latency_ms = 99.0
    with pytest.raises(ValidationError):
        PipelineResult(
            plan=plan,
            candidates=(),
            selection=selection,
            decision=decision,
            candidate_latency_ms=-1.0,
            rerank_latency_ms=0.0,
            selection_latency_ms=0.0,
            judge_latency_ms=0.0,
        )


def test_nested_search_result_remains_frozen():
    candidate = make_candidate()
    with pytest.raises(FrozenInstanceError):
        candidate.result.score = 0.1