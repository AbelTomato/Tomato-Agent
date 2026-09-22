import math
from typing import Any, Protocol

import httpx


class EmbeddingError(RuntimeError):
    pass


class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbeddingClient:
    """OpenAI-compatible `/embeddings` client with strict offline-testable validation."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        dimensions: int,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("embedding model cannot be empty")
        if dimensions <= 0:
            raise ValueError("embedding dimensions must be greater than zero")
        if timeout_seconds <= 0:
            raise ValueError("embedding timeout_seconds must be greater than zero")
        self.api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds)
        self.transport = transport

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if any(not isinstance(text, str) or not text for text in texts):
            raise ValueError("embedding input texts must be non-empty strings")

        async with httpx.AsyncClient(
            timeout=self.timeout,
            transport=self.transport,
        ) as client:
            response = await client.post(
                f"{self.base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": self.model, "input": texts},
            )
        if response.is_error:
            raise EmbeddingError(f"Embedding provider request failed: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise EmbeddingError("Embedding provider returned invalid JSON") from exc
        return self._vectors_from_response(payload, len(texts))

    def _vectors_from_response(self, payload: object, expected_count: int) -> list[list[float]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise EmbeddingError("Embedding provider returned an invalid response object")
        ordered: list[list[float] | None] = [None] * expected_count
        for item in payload["data"]:
            if not isinstance(item, dict):
                raise EmbeddingError("Embedding provider returned an invalid embedding entry")
            index, vector = item.get("index"), item.get("embedding")
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < expected_count:
                raise EmbeddingError("Embedding provider returned invalid embedding indices")
            if ordered[index] is not None:
                raise EmbeddingError("Embedding provider returned duplicate embedding indices")
            ordered[index] = self._validate_vector(vector)
        if any(vector is None for vector in ordered):
            raise EmbeddingError("Embedding provider returned incomplete embedding indices")
        return [vector for vector in ordered if vector is not None]

    def _validate_vector(self, vector: Any) -> list[float]:
        if not isinstance(vector, list) or not vector:
            raise EmbeddingError("Embedding provider returned an empty embedding")
        if len(vector) != self.dimensions:
            raise EmbeddingError(
                f"Embedding provider returned dimension {len(vector)}, expected {self.dimensions}"
            )
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in vector):
            raise EmbeddingError("Embedding provider returned a non-numeric embedding")
        normalized = [float(value) for value in vector]
        if not all(math.isfinite(value) for value in normalized):
            raise EmbeddingError("Embedding provider returned a non-finite embedding")
        return normalized