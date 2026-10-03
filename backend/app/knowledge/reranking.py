from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import Protocol

import httpx

from app.knowledge.pipeline_models import CandidateEvidence


_DIAGNOSTIC_MAX_CHARS = 300
_SENSITIVE_TEXT_RE = re.compile(
    r"(?i)(authorization\s*[:=]\s*bearer\s+[^\s,;]+|api[_ -]?key\s*[:=]\s*[^\s,;]+|bearer\s+[^\s,;]+)"
)


def _safe_diagnostic_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = _SENSITIVE_TEXT_RE.sub("[REDACTED]", value).strip()
    return text[:_DIAGNOSTIC_MAX_CHARS] or None


class RerankerError(RuntimeError):
    """Raised when the rerank provider cannot return a valid ranking."""

    def __init__(self, message: str, *, diagnostics: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def _http_diagnostics(response: httpx.Response) -> dict[str, object]:
    diagnostics: dict[str, object] = {
        "error_type": "http_error",
        "http_status": response.status_code,
    }
    request_id = response.headers.get("x-request-id") or response.headers.get("x-dashscope-request-id")
    if request_id:
        diagnostics["provider_request_id"] = _safe_diagnostic_text(request_id)
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        for source_key, target_key in (("code", "provider_code"), ("error_code", "provider_code"), ("message", "provider_message"), ("error", "provider_message")):
            if target_key not in diagnostics and source_key in body:
                value = _safe_diagnostic_text(body[source_key])
                if value is not None:
                    diagnostics[target_key] = value
    return diagnostics


def _transport_diagnostics(exc: Exception) -> dict[str, object]:
    return {"error_type": "transport_error", "exception_type": type(exc).__name__}


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
            raise RerankerError(
                "rerank provider request failed",
                diagnostics=_transport_diagnostics(exc),
            ) from exc

        if response.is_error:
            raise RerankerError(
                f"rerank provider request failed: HTTP {response.status_code}",
                diagnostics=_http_diagnostics(response),
            )
        try:
            response_payload = response.json()
        except ValueError as exc:
            raise RerankerError("rerank provider returned invalid JSON") from exc
        return _rank_from_response(response_payload, candidates, score_key="score")


class DashScopeReranker:
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
            "input": {
                "query": query,
                "documents": [candidate.result.text for candidate in candidates],
            },
            "parameters": {"top_n": len(candidates), "return_documents": True},
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    f"{self.base_url}/api/v1/services/rerank/text-rerank/text-rerank",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except Exception as exc:
            raise RerankerError(
                "rerank provider request failed",
                diagnostics=_transport_diagnostics(exc),
            ) from exc
        if response.is_error:
            raise RerankerError(
                f"rerank provider request failed: HTTP {response.status_code}",
                diagnostics=_http_diagnostics(response),
            )
        try:
            response_payload = response.json()
        except ValueError as exc:
            raise RerankerError("rerank provider returned invalid JSON") from exc
        return _rank_from_response(response_payload, candidates, score_key="relevance_score")


def _rank_from_response(
    payload: object,
    candidates: Sequence[CandidateEvidence],
    *,
    score_key: str,
) -> list[CandidateEvidence]:
    if isinstance(payload, dict) and isinstance(payload.get("output"), dict):
        payload = payload["output"]
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
        score = item.get(score_key)
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
        value = float(score)
        ranked.append((index, value, candidates[index].model_copy(update={"rerank_score": value})))
    if seen != set(range(len(candidates))):
        raise RerankerError("rerank provider response has incomplete indices")
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return [item[2] for item in ranked]