import hashlib
import json
from pathlib import Path

import aiosqlite
import pytest

from app.knowledge.ingestion import ingest_manifest
from app.knowledge.models import Chunk, Document, EmbeddingRecord
from app.knowledge.retrieval import IndexRebuildRequired
from app.knowledge.repository import KnowledgeRepository
from app.sessions.repository import SessionRepository


def write_manifest(root: Path, entries: list[dict[str, str | None]]) -> Path:
    path = root / "manifest.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_knowledge_init_migrates_index_error_on_existing_database(tmp_path: Path):
    database = tmp_path / "legacy.db"
    async with aiosqlite.connect(database) as db:
        await db.execute(
            """
            CREATE TABLE documents (
                document_id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL UNIQUE,
                source_url TEXT,
                title TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                index_status TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )
        await db.execute(
            """
            INSERT INTO documents (
                document_id, source_path, source_url, title, content_hash, index_status
            ) VALUES ('legacy', 'legacy.md', NULL, 'Legacy', 'version', 'pending')
            """
        )
        await db.commit()

    repository = KnowledgeRepository(database)
    await repository.init()

    assert await repository.get_document_index_state("legacy") == ("pending", None)
    await repository.set_document_index_state("legacy", "failed", "provider unavailable")
    assert await repository.get_document_index_state("legacy") == (
        "failed",
        "provider unavailable",
    )
    async with aiosqlite.connect(database) as db:
        cursor = await db.execute("PRAGMA table_info(documents)")
        columns = {row[1] for row in await cursor.fetchall()}
    assert "index_error" in columns


@pytest.mark.asyncio
async def test_read_only_knowledge_repository_never_mutates_database(tmp_path: Path):
    database = tmp_path / "knowledge.db"
    writer = KnowledgeRepository(database)
    await writer.init()
    await writer.replace_document(
        Document(
            document_id="read-only-doc",
            source_path="read-only.md",
            source_url=None,
            title="Read-only",
            content_hash="v1",
        ),
        [],
    )
    original_bytes = database.read_bytes()

    reader = KnowledgeRepository(database, read_only=True)
    await reader.init()

    assert await reader.get_document_by_path("read-only.md") is not None
    with pytest.raises(aiosqlite.OperationalError):
        await reader.replace_document(
            Document(
                document_id="unexpected-write",
                source_path="unexpected.md",
                source_url=None,
                title="Unexpected",
                content_hash="v2",
            ),
            [],
        )
    assert database.read_bytes() == original_bytes


@pytest.mark.asyncio
async def test_ingest_same_manifest_twice_is_idempotent(tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "one.md").write_text("# One\n\nFirst content.", encoding="utf-8")
    manifest = write_manifest(root, [{"path": "one.md", "title": "One", "url": None}])
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    await repository.init()

    first = await ingest_manifest(repository, manifest, root)
    second = await ingest_manifest(repository, manifest, root)

    assert first.succeeded == second.succeeded == 1
    assert second.skipped == 1
    assert await repository.count_documents() == 1
    assert await repository.count_chunks() == 1


@pytest.mark.asyncio
async def test_search_returns_ranked_chunks_with_citation_metadata(tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "title.md").write_text(
        "# Redis缓存\n\n本文介绍一个无关主题。", encoding="utf-8"
    )
    (root / "body.md").write_text(
        "# Python基础\n\n本文正文讨论 Redis 缓存的过期策略。", encoding="utf-8"
    )
    manifest = write_manifest(
        root,
        [
            {"path": "title.md", "title": "Redis缓存", "url": "https://example.com/title"},
            {"path": "body.md", "title": "Python基础", "url": "https://example.com/body"},
        ],
    )
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    await repository.init()
    await ingest_manifest(repository, manifest, root)

    results = await repository.search_chunks("Redis 缓存", limit=2)

    assert len(results) == 2
    assert results[0].title == "Redis缓存"
    assert results[0].source_url == "https://example.com/title"
    assert results[0].heading_path == "Redis缓存"
    assert results[0].start_line == 1
    assert results[0].end_line == 3
    assert results[0].document_version
    assert results[0].text == "本文介绍一个无关主题。"
    assert results[0].score > results[1].score


@pytest.mark.asyncio
async def test_search_prioritizes_heading_path_and_handles_empty_queries(tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "heading.md").write_text(
        "# Python\n\n## Redis缓存\n\n介绍连接池。", encoding="utf-8"
    )
    (root / "body.md").write_text(
        "# 其他主题\n\n正文介绍 Redis 缓存，但标题没有关键词。", encoding="utf-8"
    )
    manifest = write_manifest(
        root,
        [
            {"path": "heading.md", "title": "Python", "url": None},
            {"path": "body.md", "title": "其他主题", "url": None},
        ],
    )
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    await repository.init()
    await ingest_manifest(repository, manifest, root)

    results = await repository.search_chunks("Redis缓存", limit=1)

    assert len(results) == 1
    assert results[0].source_path == "heading.md"
    assert results[0].heading_path == "Python > Redis缓存"
    assert await repository.search_chunks("   ") == []
    assert await repository.search_chunks("不存在的关键词") == []


@pytest.mark.asyncio
async def test_embedding_records_persist_metadata_and_update_existing_vector(tmp_path: Path):
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    await repository.init()
    document = Document("doc", "doc.md", "https://example.test/doc", "文档", "v1")
    chunk = Chunk("chunk", "doc", "v1", "章节", 1, 2, "正文", 2)
    await repository.replace_document(document, [chunk])

    record = EmbeddingRecord(
        "chunk", "doc", "v1", "test-embedding", 3, "text-v1", (0.1, 0.2, 0.3)
    )
    await repository.upsert_embeddings([record])
    assert await repository.list_embeddings() == [record]

    replacement = EmbeddingRecord(
        "chunk", "doc", "v1", "test-embedding", 3, "text-v1", (0.4, 0.5, 0.6)
    )
    await repository.upsert_embeddings([replacement])
    assert await repository.list_embeddings() == [replacement]


@pytest.mark.asyncio
async def test_replacing_document_removes_old_embeddings(tmp_path: Path):
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    await repository.init()
    await repository.replace_document(
        Document("doc", "doc.md", None, "文档", "v1"),
        [Chunk("chunk-v1", "doc", "v1", "章节", 1, 2, "旧正文", 2)],
    )
    await repository.upsert_embeddings(
        [EmbeddingRecord("chunk-v1", "doc", "v1", "model", 2, "hash-v1", (1.0, 0.0))]
    )

    await repository.replace_document(
        Document("doc", "doc.md", None, "文档", "v2"),
        [Chunk("chunk-v2", "doc", "v2", "章节", 1, 2, "新正文", 2)],
    )

    assert await repository.list_embeddings() == []


@pytest.mark.asyncio
async def test_embedding_records_reject_dimension_mismatch(tmp_path: Path):
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    await repository.init()
    await repository.replace_document(
        Document("doc", "doc.md", None, "文档", "v1"),
        [Chunk("chunk", "doc", "v1", "章节", 1, 2, "正文", 2)],
    )

    with pytest.raises(ValueError, match="dimension"):
        await repository.upsert_embeddings(
            [EmbeddingRecord("chunk", "doc", "v1", "model", 3, "hash", (1.0, 0.0))]
        )


@pytest.mark.asyncio
async def test_repository_supports_vector_and_hybrid_search(tmp_path: Path):
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    await repository.init()
    first = Document("first", "first.md", None, "第一篇", "v1")
    second = Document("second", "second.md", None, "第二篇", "v1")
    await repository.replace_document(
        first,
        [Chunk("first-chunk", "first", "v1", "章节", 1, 2, "Redis 缓存", 2)],
    )
    await repository.replace_document(
        second,
        [Chunk("second-chunk", "second", "v1", "章节", 1, 2, "其他内容", 2)],
    )
    await repository.upsert_embeddings(
        [
            EmbeddingRecord("first-chunk", "first", "v1", "model", 2, "hash-1", (1.0, 0.0)),
            EmbeddingRecord("second-chunk", "second", "v1", "model", 2, "hash-2", (0.0, 1.0)),
        ]
    )

    vector = await repository.search_vector_chunks(
        (1.0, 0.0), model="model", dimensions=2, limit=2
    )
    hybrid = await repository.search_hybrid_chunks(
        "Redis 缓存", (0.0, 1.0), model="model", dimensions=2, limit=2
    )

    assert [result.chunk_id for result in vector] == ["first-chunk", "second-chunk"]
    assert len({result.chunk_id for result in hybrid}) == 2
    assert [result.chunk_id for result in hybrid] == ["first-chunk", "second-chunk"]

    gated_hybrid = await repository.search_hybrid_chunks(
        "Redis 缓存",
        (0.8, 0.6),
        model="model",
        dimensions=2,
        limit=2,
        min_vector_score=0.9,
    )
    assert gated_hybrid == []

    with pytest.raises(IndexRebuildRequired, match="rebuild"):
        await repository.search_vector_chunks(
            (1.0, 0.0), model="different-model", dimensions=2, limit=2
        )


@pytest.mark.asyncio
async def test_updating_one_document_replaces_only_its_chunks(tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "one.md").write_text("# One\n\nOld content.", encoding="utf-8")
    (root / "two.md").write_text("# Two\n\nStable content.", encoding="utf-8")
    manifest = write_manifest(
        root,
        [
            {"path": "one.md", "title": "One", "url": None},
            {"path": "two.md", "title": "Two", "url": None},
        ],
    )
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    await repository.init()
    await ingest_manifest(repository, manifest, root)
    before = await repository.list_chunks()

    (root / "one.md").write_text("# One\n\nNew content.", encoding="utf-8")
    report = await ingest_manifest(repository, manifest, root)
    after = await repository.list_chunks()

    assert report.updated == 1
    assert len(after) == len(before)
    assert any(chunk.text == "New content." for chunk in after)
    assert any(chunk.text == "Stable content." for chunk in after)
    assert not any(chunk.text == "Old content." for chunk in after)


@pytest.mark.asyncio
async def test_failed_import_preserves_previous_document_version(tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    document = root / "one.md"
    document.write_text("# One\n\nStable content.", encoding="utf-8")
    manifest = write_manifest(root, [{"path": "one.md", "title": "One", "url": None}])
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    await repository.init()
    await ingest_manifest(repository, manifest, root)
    original_hash = hashlib.sha256(document.read_bytes()).hexdigest()

    document.write_bytes(b"# One\n\xff")
    report = await ingest_manifest(repository, manifest, root)
    stored = await repository.get_document_by_path("one.md")

    assert report.failed == 1
    assert stored is not None
    assert stored.content_hash == original_hash
    assert (await repository.list_chunks())[0].text == "Stable content."


@pytest.mark.asyncio
async def test_knowledge_tables_do_not_break_existing_session_tables(tmp_path: Path):
    database = tmp_path / "shared.db"
    sessions = SessionRepository(database)
    await sessions.init()
    session_id = await sessions.create_session()

    knowledge = KnowledgeRepository(database)
    await knowledge.init()

    assert await sessions.session_exists(session_id)
    async with aiosqlite.connect(database) as db:
        tables = {
            row[0]
            async for row in await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {"sessions", "runs", "events", "checkpoints", "documents", "chunks"} <= tables


@pytest.mark.asyncio
async def test_sync_dry_run_lists_candidates_without_deleting(tmp_path: Path, capsys):
    from argparse import Namespace

    from app.knowledge.cli import run_sync

    root = tmp_path / "docs"
    root.mkdir()
    (root / "one.md").write_text("# One\n\ncontent", encoding="utf-8")
    manifest = write_manifest(root, [{"path": "one.md", "title": "One", "url": None}])
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    await repository.init()
    await ingest_manifest(repository, manifest, root)
    (root / "two.md").write_text("# Two\n\ncontent", encoding="utf-8")
    expanded_manifest = write_manifest(
        root,
        [
            {"path": "one.md", "title": "One", "url": None},
            {"path": "two.md", "title": "Two", "url": None},
        ],
    )
    await ingest_manifest(repository, expanded_manifest, root)
    manifest = write_manifest(root, [{"path": "one.md", "title": "One", "url": None}])

    exit_code = await run_sync(
        Namespace(
            database=str(repository.path),
            manifest=str(manifest),
            root=str(root),
            dry_run=True,
            confirm_remove=False,
        )
    )

    assert exit_code == 0
    assert "candidate-remove: two.md" in capsys.readouterr().out
    assert [document.source_path for document in await repository.list_documents()] == [
        "one.md",
        "two.md",
    ]


@pytest.mark.asyncio
async def test_sync_requires_explicit_confirmation_to_remove(tmp_path: Path):
    from argparse import Namespace

    from app.knowledge.cli import run_sync

    root = tmp_path / "docs"
    root.mkdir()
    (root / "one.md").write_text("# One\n\ncontent", encoding="utf-8")
    manifest = write_manifest(root, [{"path": "one.md", "title": "One", "url": None}])
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    await repository.init()
    await ingest_manifest(repository, manifest, root)
    manifest.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="--confirm-remove"):
        await run_sync(
            Namespace(
                database=str(repository.path),
                manifest=str(manifest),
                root=str(root),
                dry_run=False,
                confirm_remove=False,
            )
        )
    assert await repository.count_documents() == 1