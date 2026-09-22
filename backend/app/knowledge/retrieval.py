from collections import Counter
import math
import re

from app.knowledge.models import Chunk, Document, EmbeddingRecord, SearchResult


class IndexRebuildRequired(RuntimeError):
    """Raised when the stored vector index cannot answer with the requested config."""


ASCII_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*|\d+")
HAN_TOKEN = re.compile(r"[\u4e00-\u9fff]+")


def _tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for match in ASCII_TOKEN.finditer(value.casefold()):
        token = match.group(0)
        tokens.append(token)
        tokens.extend(part for part in re.split(r"[._-]", token) if part)
    for match in HAN_TOKEN.finditer(value):
        text = match.group(0)
        tokens.extend(text[index : index + 2] for index in range(len(text) - 1))
    return tokens


def _bm25(term_frequency: int, field_length: int, average_length: float, inverse_document_frequency: float) -> float:
    if term_frequency == 0:
        return 0.0
    k1 = 1.2
    b = 0.75
    length_factor = 1 - b + b * field_length / max(average_length, 1.0)
    return inverse_document_frequency * (term_frequency * (k1 + 1)) / (
        term_frequency + k1 * length_factor
    )


def _search_result(chunk: Chunk, document: Document, score: float) -> SearchResult:
    return SearchResult(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        document_version=chunk.document_version,
        source_path=document.source_path,
        source_url=document.source_url,
        title=document.title,
        heading_path=chunk.heading_path,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        text=chunk.text,
        score=score,
    )


class KeywordRetriever:
    """Offline keyword baseline using identifier tokens and adjacent Chinese bigrams."""

    def search(
        self,
        documents: list[Document],
        chunks: list[Chunk],
        query: str,
        *,
        limit: int = 5,
    ) -> list[SearchResult]:
        if limit <= 0:
            return []
        query_tokens = list(dict.fromkeys(_tokens(query)))
        if not query_tokens:
            return []
        query_han_tokens = [
            token
            for match in HAN_TOKEN.finditer(query)
            for token in (match.group(0)[index : index + 2] for index in range(len(match.group(0)) - 1))
        ]

        document_by_id = {document.document_id: document for document in documents}
        fields = [
            (
                chunk,
                document_by_id[chunk.document_id],
                Counter(_tokens(document_by_id[chunk.document_id].title)),
                Counter(_tokens(chunk.heading_path)),
                Counter(_tokens(chunk.text)),
            )
            for chunk in chunks
            if chunk.document_id in document_by_id
        ]
        if not fields:
            return []

        document_frequency = {
            token: sum(
                token in title or token in heading or token in text
                for _, _, title, heading, text in fields
            )
            for token in query_tokens
        }
        total = len(fields)
        idf = {
            token: math.log(1 + (total - frequency + 0.5) / (frequency + 0.5))
            for token, frequency in document_frequency.items()
        }
        averages = {
            "title": sum(len(title) for _, _, title, _, _ in fields) / total,
            "heading": sum(len(heading) for _, _, _, heading, _ in fields) / total,
            "text": sum(len(text) for _, _, _, _, text in fields) / total,
        }

        results: list[SearchResult] = []
        for chunk, document, title, heading, text in fields:
            if query_han_tokens and not all(
                token in title or token in heading or token in text
                for token in query_han_tokens
            ):
                continue
            score = (
                sum(_bm25(title[token], len(title), averages["title"], idf[token]) for token in query_tokens) * 3
                + sum(_bm25(heading[token], len(heading), averages["heading"], idf[token]) for token in query_tokens) * 2
                + sum(_bm25(text[token], len(text), averages["text"], idf[token]) for token in query_tokens)
            )
            if score == 0:
                continue
            results.append(_search_result(chunk, document, score))

        results.sort(
            key=lambda result: (
                -result.score,
                result.source_path,
                result.start_line,
                result.chunk_id,
            )
        )
        return results[:limit]


class VectorRetriever:
    def search(
        self,
        documents: list[Document],
        chunks: list[Chunk],
        records: list[EmbeddingRecord],
        query_vector: tuple[float, ...] | list[float],
        *,
        model: str,
        dimensions: int,
        limit: int = 5,
        min_score: float | None = None,
    ) -> list[SearchResult]:
        if limit <= 0:
            return []
        if min_score is not None and not math.isfinite(min_score):
            raise ValueError("minimum vector score must be finite")
        if not model.strip() or dimensions <= 0:
            raise ValueError("vector search model and dimensions must be valid")
        if len(query_vector) != dimensions or not all(
            math.isfinite(value) for value in query_vector
        ):
            raise ValueError("query vector must have the requested dimension and finite values")
        query_norm = math.sqrt(sum(value * value for value in query_vector))
        if query_norm == 0:
            raise ValueError("query vector must not be zero")

        relevant = [record for record in records if record.model == model and record.dimensions == dimensions]
        if records and not relevant:
            raise IndexRebuildRequired("embedding index configuration is incompatible; rebuild required")
        if not relevant:
            return []

        document_by_id = {document.document_id: document for document in documents}
        chunk_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        results: list[SearchResult] = []
        for record in relevant:
            chunk = chunk_by_id.get(record.chunk_id)
            document = document_by_id.get(record.document_id)
            if chunk is None or document is None:
                continue
            if (
                chunk.document_id != record.document_id
                or chunk.document_version != record.document_version
                or len(record.vector) != dimensions
            ):
                continue
            vector_norm = math.sqrt(sum(value * value for value in record.vector))
            if vector_norm == 0:
                continue
            score = sum(
                query_value * vector_value
                for query_value, vector_value in zip(query_vector, record.vector)
            ) / (query_norm * vector_norm)
            if min_score is not None and score < min_score:
                continue
            results.append(_search_result(chunk, document, score))

        results.sort(
            key=lambda result: (
                -result.score,
                result.source_path,
                result.start_line,
                result.chunk_id,
            )
        )
        return results[:limit]


def reciprocal_rank_fusion(
    ranked_lists: list[list[SearchResult]], *, limit: int = 5, rank_constant: int = 60
) -> list[SearchResult]:
    if limit <= 0:
        return []
    if rank_constant <= 0:
        raise ValueError("rank_constant must be positive")
    fused: dict[str, tuple[SearchResult, float]] = {}
    for ranked_results in ranked_lists:
        seen: set[str] = set()
        for rank, result in enumerate(ranked_results, start=1):
            if result.chunk_id in seen:
                continue
            seen.add(result.chunk_id)
            previous = fused.get(result.chunk_id)
            score = (previous[1] if previous else 0.0) + 1.0 / (rank_constant + rank)
            fused[result.chunk_id] = (previous[0] if previous else result, score)
    results = [
        SearchResult(**{**result.__dict__, "score": score})
        for result, score in fused.values()
    ]
    results.sort(
        key=lambda result: (
            -result.score,
            result.source_path,
            result.start_line,
            result.chunk_id,
        )
    )
    return results[:limit]