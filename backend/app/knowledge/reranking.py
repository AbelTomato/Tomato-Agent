from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

import httpx

from app.knowledge.pipeline_models import CandidateEvidence


class RerankerError(RuntimeError):
    """Raised when the rerank provider cannot return a valid ranking."""


class Reranker(Protocol):
    async def rank(
        self, query: str, candidates: Sequence[CandidateEvidence]
    ) -> list[CandidateEvidence]:
        ...


class NoopReranker:
    async def rank(
        self, query: str, candidates: Sequence[CandidateEvidence]
    ) -> list[CandidateEvidence]:
        return [candidate.model_copy(update={"rerank_score": None}) for candidate in candidates]


class CompatibleReranker:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("rerank model cannot be empty")
        if timeout_seconds <= 0:
            raise ValueError("rerank timeout_seconds must be greater than zero")
        if not base_url.strip():
            raise ValueError("rerank base_url cannot be empty")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = httpx.Timeout(timeout_seconds)
        self.transport = transport

    async def rank(
        self, query: str, candidates: Sequence[CandidateEvidence]
    ) -> list[CandidateEvidence]:
        if not query.strip():
            raise ValueError("rerank query cannot be empty")
        if not candidates:
            return []
        payload = {
            "model": self.model,
            "query": query,
            "candidates": [
                {"index": index, "text": candidate.result.text}
                for index, candidate in enumerate(candidates)
            ],
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    f"{self.base_url}/rerank",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except Exception as exc:
            raise RerankerError("rerank provider request failed") from exc

        if response.is_error:
            raise RerankerError(f"rerank provider request failed: HTTP {response.status_code}")
        try:
            response_payload = response.json()
        except ValueError as exc:
            raise RerankerError("rerank provider returned invalid JSON") from exc
        return self._rank_from_response(response_payload, candidates)

    def _rank_from_response(
        self,
        payload: object,
        candidates: Sequence[CandidateEvidence],
    ) -> list[CandidateEvidence]:
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise RerankerError("rerank provider returned an invalid response object")
        raw_results = payload["results"]
        if len(raw_results) != len(candidates):
            raise RerankerError("rerank provider response has an invalid result count")

        ranked: list[tuple[int, float, CandidateEvidence]] = []
        seen: set[int] = set()
        for item in raw_results:
            if not isinstance(item, dict):
                raise RerankerError("rerank provider returned an invalid response entry")
            index = item.get("index")
            score = item.get("score")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < len(candidates)
                or index in seen
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(float(score))
                or float(score) < 0
            ):
                raise RerankerError("rerank provider returned an invalid response entry")
            seen.add(index)
            ranked.append((index, float(score), candidates[index].model_copy(update={"rerank_score": float(score)})))
        if seen != set(range(len(candidates))):
            raise RerankerError("rerank provider response has incomplete indices")
        ranked.sort(key=lambda item: (-item[1], item[0]))
        return [item[2] for item in ranked]