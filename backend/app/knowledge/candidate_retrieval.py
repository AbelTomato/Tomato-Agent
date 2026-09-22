from __future__ import annotations

import inspect
import math
from collections.abc import Awaitable, Callable, Sequence
from typing import Literal, Protocol

from app.knowledge.models import SearchResult
from app.knowledge.pipeline_models import CandidateEvidence, QueryPlan


RetrievalMode = Literal["keyword", "vector", "hybrid"]
QueryEmbedder = Callable[[list[str]], Awaitable[list[list[float]]]]


class CandidateRepository(Protocol):
    async def search_chunks(self, query: str, limit: int = 5) -> list[SearchResult]:
        ...

    async def search_vector_chunks(
        self,
        query_vector: Sequence[float],
        *,
        model: str,
        dimensions: int,
        limit: int = 5,
        min_score: float | None = None,
    ) -> list[SearchResult]:
        ...

    async def search_hybrid_chunks(
        self,
        query: str,
        query_vector: Sequence[float],
        *,
        model: str,
        dimensions: int,
        limit: int = 5,
        min_vector_score: float | None = None,
        candidate_mode: bool = False,
    ) -> list[SearchResult]:
        ...


class CandidateRetriever(Protocol):
    async def retrieve(
        self,
        plan: QueryPlan,
        *,
        mode: RetrievalMode,
        candidate_limit: int,
    ) -> tuple[CandidateEvidence, ...]:
        ...


class RepositoryCandidateRetriever:
    def __init__(
        self,
        repository: CandidateRepository,
        *,
        query_embedder: QueryEmbedder | None = None,
        embedding_model: str = "",
        embedding_dimensions: int = 0,
        candidate_min_vector_similarity: float | None = 0.2,
    ) -> None:
        if candidate_min_vector_similarity is not None and (
            not math.isfinite(candidate_min_vector_similarity)
            or not 0.0 <= candidate_min_vector_similarity <= 1.0
        ):
            raise ValueError("candidate minimum vector similarity must be between 0 and 1")
        self.repository = repository
        self.query_embedder = query_embedder
        self.embedding_model = embedding_model
        self.embedding_dimensions = embedding_dimensions
        self.candidate_min_vector_similarity = candidate_min_vector_similarity

    async def retrieve(
        self,
        plan: QueryPlan,
        *,
        mode: RetrievalMode,
        candidate_limit: int,
    ) -> tuple[CandidateEvidence, ...]:
        if candidate_limit <= 0:
            raise ValueError("candidate_limit must be greater than zero")
        if mode not in {"keyword", "vector", "hybrid"}:
            raise ValueError(f"unsupported retrieval mode: {mode}")

        query_vectors = await self._query_vectors(plan, mode)
        merged: dict[str, CandidateEvidence] = {}
        for query_index, query in enumerate(plan.queries):
            results = await self._retrieve_query(
                query.text,
                query_vectors[query_index] if query_vectors else None,
                mode=mode,
                candidate_limit=candidate_limit,
            )
            score_type = {"keyword": "bm25", "vector": "cosine", "hybrid": "rrf"}[mode]
            for rank, result in enumerate(results, start=1):
                existing = merged.get(result.chunk_id)
                if existing is None:
                    merged[result.chunk_id] = CandidateEvidence(
                        result=result,
                        query_ids=(query.query_id,),
                        retrieval_ranks=(rank,),
                        retrieval_score_type=score_type,
                    )
                elif query.query_id not in existing.query_ids:
                    merged[result.chunk_id] = existing.model_copy(
                        update={
                            "query_ids": (*existing.query_ids, query.query_id),
                            "retrieval_ranks": (*existing.retrieval_ranks, rank),
                        }
                    )
        return tuple(merged.values())

    async def _query_vectors(
        self, plan: QueryPlan, mode: RetrievalMode
    ) -> list[list[float]] | None:
        if mode == "keyword":
            return None
        if self.query_embedder is None:
            raise ValueError("query_embedder is required for candidate vector retrieval")
        if not self.embedding_model or self.embedding_dimensions <= 0:
            raise ValueError("embedding model and dimensions are required for candidate vector retrieval")
        vectors = self.query_embedder([query.text for query in plan.queries])
        if inspect.isawaitable(vectors):
            vectors = await vectors
        if len(vectors) != len(plan.queries):
            raise ValueError("query_embedder must return one vector per planned query")
        normalized: list[list[float]] = []
        for vector in vectors:
            if (
                not isinstance(vector, list)
                or len(vector) != self.embedding_dimensions
                or not vector
                or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in vector)
            ):
                raise ValueError("query embedding has invalid dimensions or values")
            values = [float(value) for value in vector]
            if not all(math.isfinite(value) for value in values):
                raise ValueError("query embedding must contain finite numeric values")
            normalized.append(values)
        return normalized

    async def _retrieve_query(
        self,
        query: str,
        query_vector: list[float] | None,
        *,
        mode: RetrievalMode,
        candidate_limit: int,
    ) -> list[SearchResult]:
        if mode == "keyword":
            return await self.repository.search_chunks(query, limit=candidate_limit)
        if query_vector is None:
            raise ValueError("query vector is required")
        if mode == "vector":
            return await self.repository.search_vector_chunks(
                query_vector,
                model=self.embedding_model,
                dimensions=self.embedding_dimensions,
                limit=candidate_limit,
                min_score=self.candidate_min_vector_similarity,
            )
        return await self.repository.search_hybrid_chunks(
            query,
            query_vector,
            model=self.embedding_model,
            dimensions=self.embedding_dimensions,
            limit=candidate_limit,
            min_vector_score=self.candidate_min_vector_similarity,
            candidate_mode=True,
        )