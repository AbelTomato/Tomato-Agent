import pytest

from app.knowledge.candidate_retrieval import RepositoryCandidateRetriever
from app.knowledge.models import SearchResult
from app.knowledge.pipeline_models import QueryPlan, RetrievalQuery


def result(chunk_id: str, document_id: str, score: float) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        document_id=document_id,
        document_version="v1",
        source_path=f"{document_id}.md",
        source_url=None,
        title=document_id,
        heading_path="正文",
        start_line=1,
        end_line=3,
        text=f"证据 {chunk_id}",
        score=score,
    )


def plan(*texts: str) -> QueryPlan:
    queries = tuple(
        RetrievalQuery(query_id=f"q{index}", text=text, facet=f"facet-{index}")
        for index, text in enumerate(texts, start=1)
    )
    return QueryPlan(original_query=texts[0], queries=queries, is_multi_evidence=len(queries) > 1)


class FakeRepository:
    def __init__(self, results_by_query: dict[str, list[SearchResult]], error: Exception | None = None):
        self.results_by_query = results_by_query
        self.error = error
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def search_chunks(self, query: str, limit: int = 5):
        self.calls.append(("keyword", {"query": query, "limit": limit}))
        if self.error:
            raise self.error
        return self.results_by_query.get(query, [])[:limit]

    async def search_vector_chunks(self, query_vector, *, model, dimensions, limit=5, min_score=None):
        query = str(query_vector[0])
        self.calls.append(("vector", {"query": query, "limit": limit, "min_score": min_score}))
        if self.error:
            raise self.error
        return self.results_by_query.get(query, [])[:limit]

    async def search_hybrid_chunks(
        self,
        query,
        query_vector,
        *,
        model,
        dimensions,
        limit=5,
        min_vector_score=None,
        candidate_mode=False,
    ):
        self.calls.append(
            (
                "hybrid",
                {
                    "query": query,
                    "limit": limit,
                    "min_vector_score": min_vector_score,
                    "candidate_mode": candidate_mode,
                },
            )
        )
        if self.error:
            raise self.error
        return self.results_by_query.get(query, [])[:limit]


@pytest.mark.asyncio
async def test_keyword_candidate_retrieval_uses_wide_limit_and_records_bm25_ranks():
    repository = FakeRepository({"Q/K/V": [result("low", "doc-a", 0.1)]})
    retriever = RepositoryCandidateRetriever(repository)

    candidates = await retriever.retrieve(plan("Q/K/V"), mode="keyword", candidate_limit=30)

    assert [candidate.result.chunk_id for candidate in candidates] == ["low"]
    assert candidates[0].retrieval_score_type == "bm25"
    assert candidates[0].query_ids == ("q1",)
    assert candidates[0].retrieval_ranks == (1,)
    assert candidates[0].retrieval_scores == (0.1,)
    assert repository.calls == [("keyword", {"query": "Q/K/V", "limit": 30})]


@pytest.mark.asyncio
async def test_multi_query_candidates_deduplicate_chunk_and_merge_query_ids_and_ranks():
    shared = result("shared", "doc-a", 0.9)
    second = result("second", "doc-b", 0.8)
    repository = FakeRepository({"first": [shared, second], "second": [shared]})
    retriever = RepositoryCandidateRetriever(repository)

    candidates = await retriever.retrieve(plan("first", "second"), mode="keyword", candidate_limit=2)

    assert [candidate.result.chunk_id for candidate in candidates] == ["shared", "second"]
    assert candidates[0].query_ids == ("q1", "q2")
    assert candidates[0].retrieval_ranks == (1, 1)
    assert candidates[0].retrieval_scores == (0.9, 0.9)
    assert candidates[1].query_ids == ("q1",)
    assert candidates[1].retrieval_ranks == (2,)
    assert candidates[1].retrieval_scores == (0.8,)


@pytest.mark.asyncio
async def test_vector_candidate_retrieval_allows_score_below_old_baseline_threshold():
    repository = FakeRepository({"0.2": [result("low", "doc-a", 0.2)]})
    retriever = RepositoryCandidateRetriever(
        repository,
        query_embedder=lambda texts: _vectors(texts),
        embedding_model="model",
        embedding_dimensions=1,
        candidate_min_vector_similarity=0.2,
    )

    candidates = await retriever.retrieve(plan("0.2"), mode="vector", candidate_limit=30)

    assert candidates[0].result.score == 0.2
    assert candidates[0].retrieval_score_type == "cosine"
    assert repository.calls[0][1]["min_score"] == 0.2


@pytest.mark.asyncio
async def test_hybrid_candidates_record_rrf_without_comparing_it_to_cosine():
    repository = FakeRepository({"query": [result("chunk", "doc", 0.01)]})
    retriever = RepositoryCandidateRetriever(
        repository,
        query_embedder=lambda texts: _constant_vectors(texts),
        embedding_model="model",
        embedding_dimensions=1,
        candidate_min_vector_similarity=None,
    )

    candidates = await retriever.retrieve(plan("query"), mode="hybrid", candidate_limit=30)

    assert candidates[0].retrieval_score_type == "rrf"
    assert candidates[0].result.score == 0.01
    assert repository.calls[0][1]["min_vector_score"] is None
    assert repository.calls[0][1]["candidate_mode"] is True


@pytest.mark.asyncio
async def test_candidate_limit_is_independent_from_final_limit():
    repository = FakeRepository(
        {"query": [result(f"chunk-{index}", "doc", 1.0 - index / 100) for index in range(10)]}
    )
    retriever = RepositoryCandidateRetriever(repository)

    candidates = await retriever.retrieve(plan("query"), mode="keyword", candidate_limit=7)

    assert len(candidates) == 7
    assert repository.calls[0][1]["limit"] == 7


@pytest.mark.asyncio
async def test_repository_errors_are_propagated_instead_of_becoming_empty_candidates():
    repository = FakeRepository({}, error=RuntimeError("index unavailable"))
    retriever = RepositoryCandidateRetriever(repository)

    with pytest.raises(RuntimeError, match="index unavailable"):
        await retriever.retrieve(plan("query"), mode="keyword", candidate_limit=30)


@pytest.mark.asyncio
async def test_invalid_candidate_limit_is_rejected():
    retriever = RepositoryCandidateRetriever(FakeRepository({}))

    with pytest.raises(ValueError, match="candidate_limit"):
        await retriever.retrieve(plan("query"), mode="keyword", candidate_limit=0)


async def _vectors(texts: list[str]) -> list[list[float]]:
    return [[float(text) for text in texts]]


async def _constant_vectors(texts: list[str]) -> list[list[float]]:
    return [[1.0] for _ in texts]