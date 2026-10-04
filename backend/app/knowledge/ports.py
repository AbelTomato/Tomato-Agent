from typing import Protocol

from app.knowledge.models import Chunk, SearchResult


class KnowledgeQueryPort(Protocol):
    """只读知识查询能力，供 Agent Tools 使用。"""

    async def search_chunks(
        self,
        query: str,
        *,
        limit: int = 10,
        mode: str = "keyword",
        query_embedder=None,
        **kwargs,
    ) -> list[SearchResult]:
        ...

    async def get_chunk(self, chunk_id: str) -> Chunk | None:
        ...