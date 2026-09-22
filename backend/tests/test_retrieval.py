import pytest

from app.knowledge.models import Chunk, Document, EmbeddingRecord
from app.knowledge.retrieval import (
    IndexRebuildRequired,
    KeywordRetriever,
    VectorRetriever,
    reciprocal_rank_fusion,
)


def make_document(document_id: str, title: str, source_path: str) -> Document:
    return Document(
        document_id=document_id,
        source_path=source_path,
        source_url=f"https://example.test/{document_id}",
        title=title,
        content_hash=f"{document_id}-version",
    )


def make_chunk(
    document: Document,
    chunk_id: str,
    heading_path: str,
    text: str,
    start_line: int = 1,
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        document_id=document.document_id,
        document_version=document.document_version,
        heading_path=heading_path,
        start_line=start_line,
        end_line=start_line + 2,
        text=text,
        token_count=len(text),
    )


def test_keyword_retriever_ranks_exact_identifier_and_preserves_citations():
    redis = make_document("redis", "Redis 缓存实践", "redis.md")
    python = make_document("python", "Python 缓存策略", "python.md")
    chunks = [
        make_chunk(
            redis,
            "redis-ttl",
            "Redis 缓存实践 > SETEX 与缓存过期",
            "SETEX 同时写入字符串值并设置过期时间。",
        ),
        make_chunk(
            python,
            "python-ttl",
            "Python 缓存策略 > cachetools.TTLCache",
            "cachetools.TTLCache 适合进程内的短期缓存。",
        ),
    ]

    results = KeywordRetriever().search([redis, python], chunks, "TTLCache", limit=1)

    assert [result.chunk_id for result in results] == ["python-ttl"]
    assert results[0].source_url == "https://example.test/python"
    assert results[0].document_version == "python-version"
    assert results[0].heading_path.endswith("cachetools.TTLCache")
    assert results[0].start_line == 1


def test_keyword_retriever_supports_chinese_adjacent_bigrams_and_stable_ties():
    first = make_document("a", "Python 基础", "a.md")
    second = make_document("b", "Python 基础", "b.md")
    chunks = [
        make_chunk(first, "a-1", "连接配置", "连接池需要设置超时。"),
        make_chunk(second, "b-1", "连接配置", "连接池需要设置上限。"),
    ]

    results = KeywordRetriever().search([first, second], chunks, "连接配置", limit=2)

    assert [result.source_path for result in results] == ["a.md", "b.md"]
    assert results[0].score == results[1].score
    assert KeywordRetriever().search([first], chunks, "不存在", limit=5) == []
    assert KeywordRetriever().search([first], chunks, "   ", limit=5) == []


def test_vector_retriever_ranks_by_cosine_similarity_and_keeps_citations():
    first = make_document("first", "第一篇", "first.md")
    second = make_document("second", "第二篇", "second.md")
    chunks = [
        make_chunk(first, "first-1", "章节", "第一段", start_line=3),
        make_chunk(second, "second-1", "章节", "第二段", start_line=8),
    ]
    records = [
        EmbeddingRecord("first-1", "first", "first-version", "model-a", 2, "hash-a", (1.0, 0.0)),
        EmbeddingRecord("second-1", "second", "second-version", "model-a", 2, "hash-b", (0.0, 1.0)),
    ]

    results = VectorRetriever().search(
        [first, second], chunks, records, (0.9, 0.1), model="model-a", dimensions=2, limit=2
    )

    assert [result.chunk_id for result in results] == ["first-1", "second-1"]
    assert results[0].source_url == "https://example.test/first"
    assert results[0].start_line == 3
    assert results[0].score > results[1].score


def test_vector_retriever_filters_results_below_minimum_similarity():
    first = make_document("first", "第一篇", "first.md")
    second = make_document("second", "第二篇", "second.md")
    chunks = [
        make_chunk(first, "first-1", "章节", "第一段"),
        make_chunk(second, "second-1", "章节", "第二段"),
    ]
    records = [
        EmbeddingRecord("first-1", "first", "first-version", "model-a", 2, "hash-a", (1.0, 0.0)),
        EmbeddingRecord("second-1", "second", "second-version", "model-a", 2, "hash-b", (0.0, 1.0)),
    ]

    results = VectorRetriever().search(
        [first, second],
        chunks,
        records,
        (0.8, 0.6),
        model="model-a",
        dimensions=2,
        limit=2,
        min_score=0.7,
    )

    assert [result.chunk_id for result in results] == ["first-1"]
    assert results[0].score == pytest.approx(0.8)


def test_vector_retriever_rejects_incompatible_index_and_invalid_query():
    document = make_document("doc", "文档", "doc.md")
    chunk = make_chunk(document, "chunk", "章节", "正文")
    record = EmbeddingRecord("chunk", "doc", "doc-version", "model-a", 2, "hash", (1.0, 0.0))

    with pytest.raises(IndexRebuildRequired, match="rebuild"):
        VectorRetriever().search(
            [document], [chunk], [record], (1.0, 0.0), model="model-b", dimensions=2
        )
    with pytest.raises(ValueError, match="finite"):
        VectorRetriever().search(
            [document], [chunk], [record], (float("nan"), 0.0), model="model-a", dimensions=2
        )


def test_reciprocal_rank_fusion_deduplicates_and_uses_stable_order():
    first = make_document("first", "第一篇", "first.md")
    second = make_document("second", "第二篇", "second.md")
    chunks = [
        make_chunk(first, "shared", "章节", "共享"),
        make_chunk(second, "second", "章节", "第二"),
    ]
    keyword = KeywordRetriever().search([first, second], chunks, "共享", limit=2)
    vector = [
        VectorRetriever().search(
            [first, second],
            chunks,
            [
                EmbeddingRecord("shared", "first", "first-version", "model", 2, "hash", (1.0, 0.0)),
                EmbeddingRecord("second", "second", "second-version", "model", 2, "hash", (0.8, 0.6)),
            ],
            (0.8, 0.6),
            model="model",
            dimensions=2,
            limit=2,
        )[0],
        VectorRetriever().search(
            [first, second],
            chunks,
            [
                EmbeddingRecord("shared", "first", "first-version", "model", 2, "hash", (1.0, 0.0)),
                EmbeddingRecord("second", "second", "second-version", "model", 2, "hash", (0.8, 0.6)),
            ],
            (0.8, 0.6),
            model="model",
            dimensions=2,
            limit=2,
        )[1],
    ]

    results = reciprocal_rank_fusion([keyword, vector], limit=2)

    assert [result.chunk_id for result in results] == ["shared", "second"]
    assert len({result.chunk_id for result in results}) == 2
    assert results[0].score > results[1].score
