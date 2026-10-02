from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from threading import Lock
from time import perf_counter
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from app.knowledge.pipeline_models import CandidateEvidence
from app.observability.rag_trace import canonical_json_sha256


class RerankerObservationError(RuntimeError):
    """Raised when a reranker call cannot be safely observed or budgeted."""


class RerankerObserver(Protocol):
    def emit(
        self,
        event_type: str,
        *,
        status: Literal["success", "skipped", "failed"],
        payload: dict[str, Any],
        duration_ms: float | None,
    ) -> None:
        ...


class RerankerProvider(Protocol):
    async def rank(
        self, query: str, candidates: Sequence[CandidateEvidence]
    ) -> list[CandidateEvidence]:
        ...


def _error_code(error: Exception) -> str:
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ValueError):
        return "value_error"
    return "provider_error"


def _validate_ranking(value: object, expected_count: int) -> list[CandidateEvidence]:
    if not isinstance(value, list) or len(value) != expected_count:
        raise RerankerObservationError("reranker provider returned an incomplete response")
    if any(not isinstance(candidate, CandidateEvidence) for candidate in value):
        raise RerankerObservationError("reranker provider returned an invalid response")
    return value


class RecordingReranker:
    """Record and budget calls at the reranker provider interface boundary."""

    def __init__(
        self,
        provider: RerankerProvider,
        *,
        observer: RerankerObserver | None = None,
        run_id: UUID | None = None,
        model_id: str | None = None,
        max_calls: int | None = None,
    ) -> None:
        if observer is not None:
            if not isinstance(run_id, UUID):
                raise ValueError("run_id is required when reranker observation is enabled")
            if not isinstance(model_id, str) or not model_id.strip():
                raise ValueError("model_id is required when reranker observation is enabled")
        if max_calls is not None and max_calls <= 0:
            raise ValueError("max_calls must be greater than zero")
        self.provider = provider
        self.observer = observer
        self.run_id = run_id
        self.model_id = model_id
        self.max_calls = max_calls
        self._call_count = 0
        self._lock = Lock()

    def _reserve_call(self) -> bool:
        with self._lock:
            if self.max_calls is not None and self._call_count >= self.max_calls:
                return False
            self._call_count += 1
            return True

    def _question_id(self) -> str:
        from app.observability.recording_llm_client import _question_id_var

        question_id = _question_id_var.get()
        if question_id is None:
            raise RerankerObservationError("reranker observation requires an active question scope")
        return question_id

    def _emit(
        self,
        event_type: str,
        *,
        status: Literal["success", "skipped", "failed"],
        payload: dict[str, Any],
        duration_ms: float | None,
    ) -> None:
        if self.observer is None:
            return
        try:
            self.observer.emit(event_type, status=status, payload=payload, duration_ms=duration_ms)
        except Exception as exc:
            raise RerankerObservationError("unable to record reranker event") from exc

    async def rank(
        self, query: str, candidates: Sequence[CandidateEvidence]
    ) -> list[CandidateEvidence]:
        question_id = self._question_id() if self.observer is not None else None
        if not self._reserve_call():
            if self.observer is not None:
                self._emit(
                    "reranker.skipped",
                    status="skipped",
                    payload={
                        "question_id": question_id,
                        "run_id": str(self.run_id),
                        "model_id": self.model_id,
                        "reason": "budget_exceeded",
                        "call_state": "skipped",
                    },
                    duration_ms=None,
                )
            raise RerankerObservationError("reranker call limit reached")

        call_id = str(uuid4())
        request_payload = {
            "call_id": call_id,
            "question_id": question_id,
            "run_id": str(self.run_id) if self.run_id is not None else None,
            "model_id": self.model_id,
            "candidate_count": len(candidates),
            "query_sha256": canonical_json_sha256(query),
            "call_state": "attempted",
        }
        self._emit("reranker.request", status="success", payload=request_payload, duration_ms=None)
        started = perf_counter()
        try:
            response = await self.provider.rank(query, candidates)
            ranking = _validate_ranking(response, len(candidates))
        except Exception as exc:
            duration_ms = (perf_counter() - started) * 1000
            try:
                self._emit(
                    "reranker.response",
                    status="failed",
                    payload={
                        "call_id": call_id,
                        "question_id": question_id,
                        "run_id": str(self.run_id) if self.run_id is not None else None,
                        "model_id": self.model_id,
                        "candidate_count": len(candidates),
                        "call_state": "failed",
                        "error_code": _error_code(exc),
                    },
                    duration_ms=duration_ms,
                )
            except RerankerObservationError:
                # Preserve the provider/validation exception as the API error.
                pass
            raise

        self._emit(
            "reranker.response",
            status="success",
            payload={
                "call_id": call_id,
                "question_id": question_id,
                "run_id": str(self.run_id) if self.run_id is not None else None,
                "model_id": self.model_id,
                "candidate_count": len(ranking),
                "response_sha256": canonical_json_sha256(
                    [candidate.model_dump(mode="json") for candidate in deepcopy(ranking)]
                ),
                "call_state": "succeeded",
            },
            duration_ms=(perf_counter() - started) * 1000,
        )
        return ranking


__all__ = ["RecordingReranker", "RerankerObservationError"]