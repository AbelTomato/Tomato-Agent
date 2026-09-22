import hashlib
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from app.knowledge import cli
from app.knowledge.cli import run_index
from app.knowledge.indexing import build_embedding_index
from app.knowledge.ingestion import ingest_manifest
from app.knowledge.models import Chunk, Document, EmbeddingRecord
from app.knowledge.repository import KnowledgeRepository


class FakeEmbeddingProvider:
    def __init__(self, dimensions: int = 2, *, fail: bool = False):
        self.dimensions = dimensions
        self.fail = fail
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if self.fail:
            raise RuntimeError("provider unavailable")
        return [
            [float(index + 1)] + [float(len(text))] * (self.dimensions - 1)
            for index, text in enumerate(texts)
        ]


class InvalidEmbeddingProvider:
    def __init__(self, vectors: list[list[float]]):
        self.vectors = vectors
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return self.vectors


class SingleInputOnlyEmbeddingProvider:
    def __init__(self, dimensions: int = 2):
        self.dimensions = dimensions
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if len(texts) > 1:
            raise RuntimeError("HTTP 400: batch input is unsupported")
        return [[1.0] * self.dimensions for _ in texts]


def write_manifest(root: Path) -> Path:
    manifest = root / "manifest.json"
    manifest.write_text(
        '[{"path":"one.md","title":"One","url":null}]',
        encoding="utf-8",
    )
    return manifest


@pytest.mark.asyncio
async def test_build_embedding_index_persists_vectors_and_skips_current_index(tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "one.md").write_text(
        "# One\n\nFirst paragraph.\n\nSecond paragraph.",
        encoding="utf-8",
    )
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    await repository.init()
    await ingest_manifest(repository, write_manifest(root), root)
    provider = FakeEmbeddingProvider()

    first = await build_embedding_index(
        repository,
        provider,
        model="fake-model",
        dimensions=2,
        batch_size=1,
    )

    chunks = await repository.list_chunks()
    records = await repository.list_embeddings()
    records_by_chunk = {record.chunk_id: record for record in records}
    assert first.documents_seen == 1
    assert first.indexed_documents == 1
    assert first.failed_documents == 0
    assert first.chunks_indexed == len(chunks)
    assert len(provider.calls) == len(chunks)
    assert len(records) == len(chunks)
    assert all(record.model == "fake-model" for record in records)
    assert all(
        records_by_chunk[chunk.chunk_id].document_version == chunk.document_version
        for chunk in chunks
    )
    assert all(
        records_by_chunk[chunk.chunk_id].text_hash
        == hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
        for chunk in chunks
    )
    assert await repository.get_document_index_state(chunks[0].document_id) == ("ready", None)

    calls_before_repeat = len(provider.calls)
    second = await build_embedding_index(
        repository,
        provider,
        model="fake-model",
        dimensions=2,
        batch_size=1,
    )

    assert second.indexed_documents == 0
    assert second.skipped_documents == 1
    assert len(provider.calls) == calls_before_repeat


@pytest.mark.asyncio
async def test_build_embedding_index_falls_back_to_single_inputs_after_batch_failure(
    tmp_path: Path,
):
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    await repository.init()
    document = Document("doc", "doc.md", None, "Document", "version")
    chunks = [
        Chunk("chunk-1", "doc", "version", "", 1, 1, "first", 1),
        Chunk("chunk-2", "doc", "version", "", 2, 2, "second", 1),
    ]
    await repository.replace_document(document, chunks)
    provider = SingleInputOnlyEmbeddingProvider()

    report = await build_embedding_index(
        repository,
        provider,
        model="fake-model",
        dimensions=2,
        batch_size=8,
    )

    assert report.indexed_documents == 1
    assert report.failed_documents == 0
    assert report.chunks_indexed == 2
    assert len(await repository.list_embeddings()) == 2
    assert await repository.get_document_index_state("doc") == ("ready", None)
    assert provider.calls[0] == ["first", "second"]
    assert provider.calls[1:] == [["first"], ["second"]]


@pytest.mark.asyncio
async def test_build_embedding_index_batches_and_persists_vectors_for_search(tmp_path: Path):
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    await repository.init()
    document = Document("doc", "doc.md", None, "Doc", "version")
    chunks = [
        Chunk(
            chunk_id=f"chunk-{index}",
            document_id="doc",
            document_version="version",
            heading_path="Doc",
            start_line=index + 1,
            end_line=index + 1,
            text=f"chunk text {index}",
            token_count=3,
        )
        for index in range(3)
    ]
    await repository.replace_document(document, chunks)
    provider = FakeEmbeddingProvider()

    report = await build_embedding_index(
        repository,
        provider,
        model="fake-model",
        dimensions=2,
        batch_size=2,
    )

    records = await repository.list_embeddings(document.document_id)
    results = await repository.search_vector_chunks(
        records[0].vector,
        model="fake-model",
        dimensions=2,
        limit=1,
    )
    assert report.indexed_documents == 1
    assert report.chunks_indexed == 3
    assert provider.calls == [
        ["chunk text 0", "chunk text 1"],
        ["chunk text 2"],
    ]
    assert results[0].chunk_id == records[0].chunk_id


@pytest.mark.asyncio
async def test_build_embedding_index_failure_clears_partial_vectors_and_records_state(tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "one.md").write_text("# One\n\nContent.", encoding="utf-8")
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    await repository.init()
    await ingest_manifest(repository, write_manifest(root), root)

    report = await build_embedding_index(
        repository,
        FakeEmbeddingProvider(fail=True),
        model="fake-model",
        dimensions=2,
    )

    document = (await repository.list_documents())[0]
    assert report.indexed_documents == 0
    assert report.failed_documents == 1
    assert report.failures == ["one.md: provider unavailable"]
    assert await repository.list_embeddings(document.document_id) == []
    assert await repository.get_document_index_state(document.document_id) == (
        "failed",
        "provider unavailable",
    )


@pytest.mark.asyncio
async def test_failed_reindex_removes_stale_target_vectors_but_keeps_other_configurations(
    tmp_path: Path,
):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "one.md").write_text("# One\n\nContent.", encoding="utf-8")
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    await repository.init()
    await ingest_manifest(repository, write_manifest(root), root)
    document = (await repository.list_documents())[0]
    chunk = (await repository.list_chunks(document.document_id))[0]
    await repository.upsert_embeddings(
        [
            EmbeddingRecord(
                chunk_id=chunk.chunk_id,
                document_id=document.document_id,
                document_version=chunk.document_version,
                model="fake-model",
                dimensions=2,
                text_hash="stale",
                vector=(9.0, 9.0),
            ),
            EmbeddingRecord(
                chunk_id=chunk.chunk_id,
                document_id=document.document_id,
                document_version=chunk.document_version,
                model="other-model",
                dimensions=2,
                text_hash="other",
                vector=(8.0, 8.0),
            ),
        ]
    )

    report = await build_embedding_index(
        repository,
        InvalidEmbeddingProvider([[1.0]]),
        model="fake-model",
        dimensions=2,
    )

    assert report.failed_documents == 1
    assert [record.model for record in await repository.list_embeddings(document.document_id)] == [
        "other-model"
    ]
    assert await repository.get_document_index_state(document.document_id) == (
        "failed",
        "embedding vector dimension does not match configuration: 1 != 2",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "message"),
    [
        (
            InvalidEmbeddingProvider([]),
            "embedding provider returned 0 vectors, expected 1",
        ),
        (
            InvalidEmbeddingProvider([[1.0, float("nan")]]),
            "embedding vector must contain finite numeric values",
        ),
    ],
)
async def test_index_validates_provider_count_and_finite_values(
    tmp_path: Path, provider: InvalidEmbeddingProvider, message: str
):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "one.md").write_text("# One\n\nContent.", encoding="utf-8")
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    await repository.init()
    await ingest_manifest(repository, write_manifest(root), root)

    report = await build_embedding_index(
        repository,
        provider,
        model="fake-model",
        dimensions=2,
    )

    assert report.failed_documents == 1
    assert report.failures == [f"one.md: {message}"]


@pytest.mark.asyncio
async def test_index_reports_empty_document_without_calling_provider(tmp_path: Path):
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    await repository.init()
    from app.knowledge.models import Document

    document = Document("empty", "empty.md", None, "Empty", "version")
    await repository.replace_document(document, [])
    provider = FakeEmbeddingProvider()

    report = await build_embedding_index(
        repository,
        provider,
        model="fake-model",
        dimensions=2,
    )

    assert report.failed_documents == 1
    assert report.failures == ["empty.md: document has no chunks"]
    assert provider.calls == []
    assert await repository.get_document_index_state(document.document_id) == (
        "failed",
        "document has no chunks",
    )


@pytest.mark.asyncio
async def test_index_cli_uses_fake_provider_and_returns_failure_for_invalid_vectors(
    tmp_path: Path, capsys
):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "one.md").write_text("# One\n\nContent.", encoding="utf-8")
    database = tmp_path / "knowledge.db"
    repository = KnowledgeRepository(database)
    await repository.init()
    await ingest_manifest(repository, write_manifest(root), root)

    success = await run_index(
        Namespace(
            database=str(database),
            embedding_model="fake-model",
            embedding_dimensions=2,
            batch_size=8,
        ),
        provider=FakeEmbeddingProvider(dimensions=2),
    )
    assert success == 0
    assert "indexed=1" in capsys.readouterr().out

    failure = await run_index(
        Namespace(
            database=str(database),
            embedding_model="fake-model",
            embedding_dimensions=3,
            batch_size=8,
        ),
        provider=FakeEmbeddingProvider(dimensions=2),
    )
    assert failure == 1
    assert "failed=1" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_index_cli_requires_api_key_without_injected_provider(monkeypatch, tmp_path: Path):
    from app.settings import Settings

    monkeypatch.setattr(cli, "settings", Settings(embedding_api_key=""))

    with pytest.raises(ValueError, match="embedding_api_key"):
        await run_index(
            Namespace(
                database=str(tmp_path / "knowledge.db"),
                embedding_model="fake-model",
                embedding_dimensions=2,
                batch_size=8,
            )
        )


def test_index_cli_main_prints_stable_configuration_error(monkeypatch, tmp_path: Path, capsys):
    from app.settings import Settings

    monkeypatch.setattr(cli, "settings", Settings(embedding_api_key=""))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "knowledge",
            "--database",
            str(tmp_path / "knowledge.db"),
            "index",
            "--embedding-model",
            "fake-model",
            "--embedding-dimensions",
            "2",
        ],
    )

    assert cli.main() == 1
    assert capsys.readouterr().out.strip() == (
        "error: index requires embedding_api_key in settings"
    )
