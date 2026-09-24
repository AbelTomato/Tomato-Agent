import json
import math
from pathlib import Path

import aiosqlite

from app.knowledge.models import Chunk, Document, EmbeddingRecord, SearchResult
from app.knowledge.retrieval import (
    KeywordRetriever,
    VectorRetriever,
    reciprocal_rank_fusion,
)


class KnowledgeRepository:
    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = path
        self.read_only = read_only

    def _connect(self):
        if self.read_only:
            return aiosqlite.connect(
                f"{self.path.resolve().as_uri()}?mode=ro",
                uri=True,
            )
        return aiosqlite.connect(self.path)

    async def init(self) -> None:
        if self.read_only:
            if not self.path.is_file():
                raise FileNotFoundError(f"knowledge database does not exist: {self.path}")
            async with self._connect() as db:
                await db.execute("PRAGMA query_only = ON")
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
                tables = {row[0] for row in await cursor.fetchall()}
            missing = {"documents", "chunks", "embeddings"} - tables
            if missing:
                raise ValueError(
                    f"read-only knowledge database is missing tables: {sorted(missing)}"
                )
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL UNIQUE,
                    source_url TEXT,
                    title TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    index_status TEXT NOT NULL DEFAULT 'pending',
                    index_error TEXT
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    document_version TEXT NOT NULL,
                    heading_path TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    token_count INTEGER NOT NULL,
                    FOREIGN KEY (document_id) REFERENCES documents(document_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);
                CREATE TABLE IF NOT EXISTS embeddings (
                    chunk_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    document_version TEXT NOT NULL,
                    model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    text_hash TEXT NOT NULL,
                    vector_json TEXT NOT NULL,
                    PRIMARY KEY (chunk_id, model, dimensions),
                    FOREIGN KEY (chunk_id) REFERENCES chunks(chunk_id) ON DELETE CASCADE,
                    FOREIGN KEY (document_id) REFERENCES documents(document_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_embeddings_document_id ON embeddings(document_id);
                """
            )
            cursor = await db.execute("PRAGMA table_info(documents)")
            document_columns = {row[1] for row in await cursor.fetchall()}
            if "index_error" not in document_columns:
                await db.execute("ALTER TABLE documents ADD COLUMN index_error TEXT")
            await db.commit()

    async def get_document_by_path(self, source_path: str) -> Document | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT document_id, source_path, source_url, title, content_hash "
                "FROM documents WHERE source_path = ?",
                (source_path,),
            )
            row = await cursor.fetchone()
        return None if row is None else Document(*row)

    async def get_document(self, document_id: str) -> Document | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT document_id, source_path, source_url, title, content_hash "
                "FROM documents WHERE document_id = ?",
                (document_id,),
            )
            row = await cursor.fetchone()
        return None if row is None else Document(*row)

    async def list_documents(self) -> list[Document]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT document_id, source_path, source_url, title, content_hash "
                "FROM documents ORDER BY source_path"
            )
            rows = await cursor.fetchall()
        return [Document(*row) for row in rows]

    async def list_chunks(self, document_id: str | None = None) -> list[Chunk]:
        query = (
            "SELECT chunk_id, document_id, document_version, heading_path, "
            "start_line, end_line, text, token_count FROM chunks"
        )
        parameters: tuple[str, ...] = ()
        if document_id is not None:
            query += " WHERE document_id = ?"
            parameters = (document_id,)
        query += " ORDER BY document_id, start_line, chunk_id"
        async with self._connect() as db:
            cursor = await db.execute(query, parameters)
            rows = await cursor.fetchall()
        return [Chunk(*row) for row in rows]

    async def get_chunk(self, chunk_id: str) -> Chunk | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT chunk_id, document_id, document_version, heading_path, "
                "start_line, end_line, text, token_count FROM chunks WHERE chunk_id = ?",
                (chunk_id,),
            )
            row = await cursor.fetchone()
        return None if row is None else Chunk(*row)

    async def search_chunks(self, query: str, limit: int = 5) -> list[SearchResult]:
        documents = await self.list_documents()
        chunks = await self.list_chunks()
        return KeywordRetriever().search(documents, chunks, query, limit=limit)

    async def search_vector_chunks(
        self,
        query_vector: tuple[float, ...] | list[float],
        *,
        model: str,
        dimensions: int,
        limit: int = 5,
        min_score: float | None = None,
    ) -> list[SearchResult]:
        documents = await self.list_documents()
        chunks = await self.list_chunks()
        records = await self.list_embeddings()
        return VectorRetriever().search(
            documents,
            chunks,
            records,
            query_vector,
            model=model,
            dimensions=dimensions,
            limit=limit,
            min_score=min_score,
        )

    async def search_hybrid_chunks(
        self,
        query: str,
        query_vector: tuple[float, ...] | list[float],
        *,
        model: str,
        dimensions: int,
        limit: int = 5,
        min_vector_score: float | None = None,
        candidate_mode: bool = False,
    ) -> list[SearchResult]:
        documents = await self.list_documents()
        chunks = await self.list_chunks()
        records = await self.list_embeddings()
        keyword_results = KeywordRetriever().search(documents, chunks, query, limit=limit)
        vector_results = VectorRetriever().search(
            documents,
            chunks,
            records,
            query_vector,
            model=model,
            dimensions=dimensions,
            limit=limit,
            min_score=min_vector_score,
        )
        if min_vector_score is not None and not vector_results and not candidate_mode:
            return []
        return reciprocal_rank_fusion([keyword_results, vector_results], limit=limit)

    async def count_documents(self) -> int:
        async with self._connect() as db:
            cursor = await db.execute("SELECT COUNT(*) FROM documents")
            return (await cursor.fetchone())[0]

    async def count_chunks(self) -> int:
        async with self._connect() as db:
            cursor = await db.execute("SELECT COUNT(*) FROM chunks")
            return (await cursor.fetchone())[0]

    async def get_document_index_state(self, document_id: str) -> tuple[str, str | None] | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT index_status, index_error FROM documents WHERE document_id = ?",
                (document_id,),
            )
            row = await cursor.fetchone()
        return None if row is None else (row[0], row[1])

    async def set_document_index_state(
        self,
        document_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        if status not in {"pending", "indexing", "ready", "failed"}:
            raise ValueError(f"unsupported index status: {status}")
        async with self._connect() as db:
            cursor = await db.execute("SELECT 1 FROM documents WHERE document_id = ?", (document_id,))
            if await cursor.fetchone() is None:
                raise ValueError(f"document not found: {document_id}")
            await db.execute(
                "UPDATE documents SET index_status = ?, index_error = ? WHERE document_id = ?",
                (status, error, document_id),
            )
            await db.commit()

    async def delete_embeddings(
        self,
        document_id: str,
        *,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> None:
        conditions = ["document_id = ?"]
        parameters: list[str | int] = [document_id]
        if model is not None:
            conditions.append("model = ?")
            parameters.append(model)
        if dimensions is not None:
            conditions.append("dimensions = ?")
            parameters.append(dimensions)
        async with self._connect() as db:
            await db.execute(
                f"DELETE FROM embeddings WHERE {' AND '.join(conditions)}",
                parameters,
            )
            await db.commit()

    async def list_embeddings(self, document_id: str | None = None) -> list[EmbeddingRecord]:
        query = (
            "SELECT chunk_id, document_id, document_version, model, dimensions, "
            "text_hash, vector_json FROM embeddings"
        )
        parameters: tuple[str, ...] = ()
        if document_id is not None:
            query += " WHERE document_id = ?"
            parameters = (document_id,)
        query += " ORDER BY chunk_id, model, dimensions"
        async with self._connect() as db:
            cursor = await db.execute(query, parameters)
            rows = await cursor.fetchall()
        return [
            EmbeddingRecord(
                chunk_id=row[0],
                document_id=row[1],
                document_version=row[2],
                model=row[3],
                dimensions=row[4],
                text_hash=row[5],
                vector=tuple(float(value) for value in json.loads(row[6])),
            )
            for row in rows
        ]

    async def upsert_embeddings(self, records: list[EmbeddingRecord]) -> None:
        if not records:
            return
        for record in records:
            if not record.model.strip():
                raise ValueError("embedding model must be non-empty")
            if record.dimensions <= 0:
                raise ValueError("embedding dimensions must be positive")
            if len(record.vector) != record.dimensions:
                raise ValueError("embedding vector dimension does not match metadata")
            if not record.text_hash:
                raise ValueError("embedding text_hash must be non-empty")
            if not all(math.isfinite(value) for value in record.vector):
                raise ValueError("embedding vector must contain finite values")

        async with self._connect() as db:
            await db.execute("PRAGMA foreign_keys = ON")
            try:
                await db.execute("BEGIN")
                for record in records:
                    cursor = await db.execute(
                        "SELECT document_id, document_version FROM chunks WHERE chunk_id = ?",
                        (record.chunk_id,),
                    )
                    chunk_row = await cursor.fetchone()
                    if chunk_row is None:
                        raise ValueError(f"embedding chunk does not exist: {record.chunk_id}")
                    if chunk_row != (record.document_id, record.document_version):
                        raise ValueError(
                            f"embedding metadata does not match chunk: {record.chunk_id}"
                        )
                    await db.execute(
                        """
                        INSERT INTO embeddings (
                            chunk_id, document_id, document_version, model,
                            dimensions, text_hash, vector_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(chunk_id, model, dimensions) DO UPDATE SET
                            document_id=excluded.document_id,
                            document_version=excluded.document_version,
                            text_hash=excluded.text_hash,
                            vector_json=excluded.vector_json
                        """,
                        (
                            record.chunk_id,
                            record.document_id,
                            record.document_version,
                            record.model,
                            record.dimensions,
                            record.text_hash,
                            json.dumps(record.vector, separators=(",", ":")),
                        ),
                    )
            except Exception:
                await db.rollback()
                raise
            else:
                await db.commit()

    async def replace_document(self, document: Document, chunks: list[Chunk]) -> None:
        if any(chunk.document_id != document.document_id for chunk in chunks):
            raise ValueError("chunk document_id must match its document")
        async with self._connect() as db:
            await db.execute("PRAGMA foreign_keys = ON")
            try:
                await db.execute("BEGIN")
                await db.execute(
                    """
                    INSERT INTO documents (
                        document_id, source_path, source_url, title, content_hash,
                        index_status, index_error
                    ) VALUES (?, ?, ?, ?, ?, 'pending', NULL)
                    ON CONFLICT(document_id) DO UPDATE SET
                        source_path=excluded.source_path,
                        source_url=excluded.source_url,
                        title=excluded.title,
                        content_hash=excluded.content_hash,
                        index_status='pending',
                        index_error=NULL
                    """,
                    (
                        document.document_id,
                        document.source_path,
                        document.source_url,
                        document.title,
                        document.content_hash,
                    ),
                )
                await db.execute(
                    "DELETE FROM embeddings WHERE document_id = ?", (document.document_id,)
                )
                await db.execute(
                    "DELETE FROM chunks WHERE document_id = ?", (document.document_id,)
                )
                await db.executemany(
                    """
                    INSERT INTO chunks (
                        chunk_id, document_id, document_version, heading_path,
                        start_line, end_line, text, token_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            chunk.chunk_id,
                            chunk.document_id,
                            chunk.document_version,
                            chunk.heading_path,
                            chunk.start_line,
                            chunk.end_line,
                            chunk.text,
                            chunk.token_count,
                        )
                        for chunk in chunks
                    ],
                )
            except Exception:
                await db.rollback()
                raise
            else:
                await db.commit()

    async def delete_documents(self, source_paths: list[str]) -> int:
        if not source_paths:
            return 0
        placeholders = ", ".join("?" for _ in source_paths)
        async with self._connect() as db:
            await db.execute("PRAGMA foreign_keys = ON")
            cursor = await db.execute(
                f"DELETE FROM documents WHERE source_path IN ({placeholders})", source_paths
            )
            await db.commit()
            return cursor.rowcount