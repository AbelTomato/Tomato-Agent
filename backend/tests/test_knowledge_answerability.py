import pytest
from pydantic import ValidationError

from app.knowledge.answerability import AnswerabilityConfig, CoverageAnswerabilityJudge
from app.knowledge.models import SearchResult
from app.knowledge.pipeline_models import CandidateEvidence, EvidenceSelection, QueryPlan, RetrievalQuery


def plan(*query_ids: str, multi: bool = True) -> QueryPlan:
    queries = tuple(RetrievalQuery(query_id=query_id, text=query_id, facet=query_id) for query_id in query_ids)
    return QueryPlan(original_query=query_ids[0] if query_ids else "问题", queries=queries, is_multi_evidence=multi)


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
            text="证据正文",
            score=0.8,
        ),
        query_ids=query_ids,
        retrieval_ranks=tuple(1 for _ in query_ids),
        retrieval_score_type="cosine",
    )


def selection(*items: CandidateEvidence, covered_query_ids: tuple[str, ...]) -> EvidenceSelection:
    return EvidenceSelection(
        selected=items,
        covered_query_ids=covered_query_ids,
        covered_document_ids=tuple(dict.fromkeys(item.result.document_id for item in items)),
        final_limit=max(len(items), 1),
    )


def test_judge_returns_no_results_only_when_no_evidence_is_selected():
    decision = CoverageAnswerabilityJudge().judge(
        plan("q1", multi=False),
        selection(covered_query_ids=()),
        config=AnswerabilityConfig(),
    )

    assert decision.status == "no_results"
    assert decision.reason == "no_candidates"
    assert decision.coverage_ratio == 0.0


def test_judge_returns_insufficient_for_candidates_without_required_query_coverage():
    item = evidence("chunk", "doc", ("other",))
    decision = CoverageAnswerabilityJudge().judge(
        plan("q1", multi=False),
        selection(item, covered_query_ids=()),
        config=AnswerabilityConfig(),
    )

    assert decision.status == "insufficient"
    assert decision.reason == "missing_required_query"
    assert decision.coverage_ratio == 0.0


def test_judge_rejects_partial_multi_evidence_even_when_candidate_pool_is_nonempty():
    item = evidence("q1", "doc-a", ("q1",))
    decision = CoverageAnswerabilityJudge().judge(
        plan("q1", "q2"),
        selection(item, covered_query_ids=("q1",)),
        config=AnswerabilityConfig(),
    )

    assert decision.status == "insufficient"
    assert decision.reason == "partial_coverage"
    assert decision.coverage_ratio == 0.5


def test_judge_supports_two_fully_covered_evidence_facets():
    first = evidence("q1", "doc-a", ("q1",))
    second = evidence("q2", "doc-b", ("q2",))
    decision = CoverageAnswerabilityJudge().judge(
        plan("q1", "q2"),
        selection(first, second, covered_query_ids=("q1", "q2")),
        config=AnswerabilityConfig(),
    )

    assert decision.status == "supported"
    assert decision.reason == "full_query_coverage"
    assert decision.coverage_ratio == 1.0


def test_judge_can_require_minimum_evidence_and_can_use_rerank_confidence():
    item = evidence("q1", "doc", ("q1",)).model_copy(update={"rerank_score": 0.4})
    decision = CoverageAnswerabilityJudge().judge(
        plan("q1", multi=False),
        selection(item, covered_query_ids=("q1",)),
        config=AnswerabilityConfig(min_supported_evidence=2, min_confidence=0.5),
    )

    assert decision.status == "insufficient"
    assert decision.reason == "insufficient_evidence"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_supported_coverage": -0.1},
        {"min_supported_coverage": 1.1},
        {"min_partial_coverage": 1.1},
        {"min_supported_evidence": 0},
        {"min_confidence": -0.1},
        {"multi_evidence_requires_all_queries": "yes"},
    ],
)
def test_answerability_config_rejects_invalid_values(kwargs):
    with pytest.raises(ValidationError):
        AnswerabilityConfig(**kwargs)


def test_judge_does_not_consume_old_vector_similarity_threshold():
    item = evidence("q1", "doc", ("q1",))
    decision = CoverageAnswerabilityJudge().judge(
        plan("q1", multi=False),
        selection(item, covered_query_ids=("q1",)),
        config=AnswerabilityConfig(min_confidence=None),
    )

    assert decision.status == "supported"