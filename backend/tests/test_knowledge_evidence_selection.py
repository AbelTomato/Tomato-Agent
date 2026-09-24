import pytest

from app.knowledge.evidence_selection import CoverageAwareEvidenceSelector
from app.knowledge.models import SearchResult
from app.knowledge.pipeline_models import CandidateEvidence, QueryPlan, RetrievalQuery


def candidate(chunk_id: str, document_id: str, query_ids: tuple[str, ...], rank: int) -> CandidateEvidence:
    return CandidateEvidence(
        result=SearchResult(
            chunk_id=chunk_id,
            document_id=document_id,
            document_version="v1",
            source_path=f"{document_id}.md",
            source_url=None,
            title=document_id,
            heading_path="正文",
            start_line=rank,
            end_line=rank + 1,
            text=f"证据 {chunk_id}",
            score=1.0 / rank,
        ),
        query_ids=query_ids,
        retrieval_ranks=tuple(rank for _ in query_ids),
        retrieval_score_type="cosine",
    )


def plan(*query_ids: str) -> QueryPlan:
    queries = tuple(RetrievalQuery(query_id=query_id, text=query_id, facet=query_id) for query_id in query_ids)
    return QueryPlan(original_query=query_ids[0], queries=queries, is_multi_evidence=len(queries) > 1)


def test_selector_keeps_two_documents_for_two_evidence_facets():
    candidates = [
        candidate("q1-best", "doc-q1", ("q1",), 1),
        candidate("q2-seventh", "doc-q2", ("q2",), 7),
    ]

    selection = CoverageAwareEvidenceSelector().select(plan("q1", "q2"), candidates, final_limit=5)

    assert [item.result.chunk_id for item in selection.selected] == ["q1-best", "q2-seventh"]
    assert selection.covered_query_ids == ("q1", "q2")
    assert selection.covered_document_ids == ("doc-q1", "doc-q2")


def test_selector_prefers_uncovered_query_over_duplicate_same_document_candidates():
    candidates = [
        candidate("q1-a", "doc-a", ("q1",), 1),
        candidate("q1-b", "doc-a", ("q1",), 2),
        candidate("q2-a", "doc-b", ("q2",), 3),
    ]

    selection = CoverageAwareEvidenceSelector().select(plan("q1", "q2"), candidates, final_limit=2)

    assert [item.result.chunk_id for item in selection.selected] == ["q1-a", "q2-a"]
    assert selection.covered_query_ids == ("q1", "q2")


def test_selector_uses_relevance_order_for_simple_query():
    candidates = [
        candidate("second", "doc", ("q1",), 2),
        candidate("first", "doc", ("q1",), 1),
    ]
    candidates[0] = candidates[0].model_copy(update={"rerank_score": 0.9})
    candidates[1] = candidates[1].model_copy(update={"rerank_score": 0.1})

    selection = CoverageAwareEvidenceSelector().select(plan("q1"), candidates, final_limit=1)

    assert [item.result.chunk_id for item in selection.selected] == ["second"]


def test_selector_reports_partial_coverage_when_limit_is_too_small():
    selection = CoverageAwareEvidenceSelector().select(
        plan("q1", "q2"),
        [candidate("q1", "doc-a", ("q1",), 1), candidate("q2", "doc-b", ("q2",), 2)],
        final_limit=1,
    )

    assert len(selection.selected) == 1
    assert selection.covered_query_ids == ("q1",)
    assert selection.covered_document_ids == ("doc-a",)
    assert [(item.chunk_id, item.selected, item.excluded_reason) for item in selection.dispositions] == [
        ("q1", True, None),
        ("q2", False, "final_limit"),
    ]


def test_selector_records_duplicate_chunk_exclusion_at_decision_boundary():
    duplicated = candidate("same", "doc-a", ("q1",), 1)
    selection = CoverageAwareEvidenceSelector().select(
        plan("q1"), [duplicated, duplicated], final_limit=2
    )

    assert [(item.candidate_index, item.selected, item.excluded_reason) for item in selection.dispositions] == [
        (1, True, None),
        (2, False, "duplicate_chunk"),
    ]


@pytest.mark.parametrize("final_limit", [0, -1])
def test_selector_rejects_invalid_final_limit(final_limit: int):
    with pytest.raises(ValueError, match="final_limit"):
        CoverageAwareEvidenceSelector().select(plan("q1"), [], final_limit=final_limit)