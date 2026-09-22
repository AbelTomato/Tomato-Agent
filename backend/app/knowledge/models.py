from dataclasses import dataclass, field
from hashlib import sha256


def stable_hash(*parts: str) -> str:
    return sha256("\0".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Document:
    document_id: str
    source_path: str
    source_url: str | None
    title: str
    content_hash: str

    @property
    def document_version(self) -> str:
        return self.content_hash


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    document_id: str
    document_version: str
    heading_path: str
    start_line: int
    end_line: int
    text: str
    token_count: int


@dataclass(frozen=True)
class EmbeddingRecord:
    chunk_id: str
    document_id: str
    document_version: str
    model: str
    dimensions: int
    text_hash: str
    vector: tuple[float, ...]


@dataclass(frozen=True)
class SearchResult:
    chunk_id: str
    document_id: str
    document_version: str
    source_path: str
    source_url: str | None
    title: str
    heading_path: str
    start_line: int
    end_line: int
    text: str
    score: float


@dataclass(frozen=True)
class ParsedChunk:
    heading_path: str
    start_line: int
    end_line: int
    text: str
    token_count: int


@dataclass
class IngestReport:
    succeeded: int = 0
    skipped: int = 0
    updated: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)