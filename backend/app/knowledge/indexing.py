from collections.abc import Sequence
from dataclasses import dataclass, field
from hashlib import sha256
import math

from app.knowledge.embeddings import EmbeddingProvider
from app.knowledge.models import EmbeddingRecord
from app.knowledge.repository import KnowledgeRepository


@dataclass
class IndexReport:
    documents_seen: int = 0
    indexed_documents: int = 0
    skipped_documents: int = 0
    failed_documents: int = 0
    chunks_indexed: int = 0
    failures: list[str] = field(default_factory=list)


def _text_hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _validate_vectors(
    vectors: Sequence[Sequence[float]], *, expected_count: int, dimensions: int
) -> list[tuple[float, ...]]:
    if len(vectors) != expected_count:
        raise ValueError(
            f"embedding provider returned {len(vectors)} vectors, expected {expected_count}"
        )
    normalized: list[tuple[float, ...]] = []
    for vector in vectors:
        if len(vector) != dimensions:
            raise ValueError(
                f"embedding vector dimension does not match configuration: "
                f"{len(vector)} != {dimensions}"
            )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in vector
        ):
            raise ValueError("embedding vector must contain finite numeric values")
        normalized.append(tuple(float(value) for value in vector))
    return normalized


async def _embed_batch_with_fallback(
    provider: EmbeddingProvider,
    chunks: Sequence[object],
    *,
    dimensions: int,
) -> list[tuple[float, ...]]:
    texts = [chunk.text for chunk in chunks]
    try:
        vectors = await provider.embed(texts)
    except Exception:
        if len(texts) == 1:
            raise
        vectors = []
        for text in texts:
            single_vectors = await provider.embed([text])
            vectors.extend(
                _validate_vectors(single_vectors, expected_count=1, dimensions=dimensions)
            )
        return vectors
    return _validate_vectors(vectors, expected_count=len(texts), dimensions=dimensions)


async def build_embedding_index(
    repository: KnowledgeRepository,
    provider: EmbeddingProvider,
    *,
    model: str,
    dimensions: int,
    batch_size: int = 32,
) -> IndexReport:
    if not model.strip():
        raise ValueError("embedding model cannot be empty")
    if dimensions <= 0:
        raise ValueError("embedding dimensions must be greater than zero")
    if batch_size <= 0:
        raise ValueError("embedding batch_size must be greater than zero")

    report = IndexReport()
    documents = await repository.list_documents()
    report.documents_seen = len(documents)
    for document in documents:
        chunks = await repository.list_chunks(document.document_id)
        state = await repository.get_document_index_state(document.document_id)
        existing = [
            record
            for record in await repository.list_embeddings(document.document_id)
            if record.model == model and record.dimensions == dimensions
        ]
        existing_by_chunk = {record.chunk_id: record for record in existing}
        index_is_current = len(existing) == len(chunks) and all(
            (record := existing_by_chunk.get(chunk.chunk_id)) is not None
            and record.document_version == chunk.document_version
            and record.text_hash == _text_hash(chunk.text)
            for chunk in chunks
        )
        if state is not None and state[0] == "ready" and index_is_current:
            report.skipped_documents += 1
            continue

        try:
            await repository.set_document_index_state(document.document_id, "indexing")
            await repository.delete_embeddings(
                document.document_id, model=model, dimensions=dimensions
            )
            records: list[EmbeddingRecord] = []
            for start in range(0, len(chunks), batch_size):
                batch = chunks[start : start + batch_size]
                normalized = await _embed_batch_with_fallback(
                    provider,
                    batch,
                    dimensions=dimensions,
                )
                records.extend(
                    EmbeddingRecord(
                        chunk_id=chunk.chunk_id,
                        document_id=chunk.document_id,
                        document_version=chunk.document_version,
                        model=model,
                        dimensions=dimensions,
                        text_hash=_text_hash(chunk.text),
                        vector=vector,
                    )
                    for chunk, vector in zip(batch, normalized, strict=True)
                )
            if not chunks:
                raise ValueError("document has no chunks")
            await repository.upsert_embeddings(records)
            await repository.set_document_index_state(document.document_id, "ready")
            report.indexed_documents += 1
            report.chunks_indexed += len(records)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            await repository.set_document_index_state(document.document_id, "failed", message)
            report.failed_documents += 1
            report.failures.append(f"{document.source_path}: {message}")
    return report
