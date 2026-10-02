from __future__ import annotations

import math
from copy import deepcopy
from threading import Lock
from time import perf_counter
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from app.observability.rag_trace import canonical_json_sha256


class EmbeddingObservationError(RuntimeError):
    """Raised when an embedding call cannot be safely observed or budgeted."""


class EmbeddingObserver(Protocol):
    def emit(
        self,
        event_type: str,
        *,
        status: Literal["success", "skipped", "failed"],
        payload: dict[str, Any],
        duration_ms: float | None,
    ) -> None:
        ...


class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        ...


def _error_code(error: Exception) -> str:
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ValueError):
        return "value_error"
    return "provider_error"


def _validate_vectors(value: object, expected_count: int) -> tuple[int, int | None]:
    if not isinstance(value, list) or len(value) != expected_count:
        raise EmbeddingObservationError("embedding provider returned an incomplete response")
    dimensions: int | None = None
    for vector in value:
        if not isinstance(vector, list) or not vector:
            raise EmbeddingObservationError("embedding provider returned an invalid vector")
        if dimensions is None:
            dimensions = len(vector)
        if len(vector) != dimensions:
            raise EmbeddingObservationError("embedding provider returned inconsistent dimensions")
        if any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            for item in vector
        ):
            raise EmbeddingObservationError("embedding provider returned an invalid vector")
    return len(value), dimensions


class RecordingEmbeddingClient:
    """Record and budget calls at the embedding provider interface boundary."""

    def __init__(
        self,
        provider: EmbeddingProvider,
        *,
        observer: EmbeddingObserver | None = None,
        run_id: UUID | None = None,
        model_id: str | None = None,
        max_calls: int | None = None,
    ) -> None:
        if observer is not None:
            if not isinstance(run_id, UUID):
                raise ValueError("run_id is required when embedding observation is enabled")
            if not isinstance(model_id, str) or not model_id.strip():
                raise ValueError("model_id is required when embedding observation is enabled")
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
            raise EmbeddingObservationError("embedding observation requires an active question scope")
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
            raise EmbeddingObservationError("unable to record embedding event") from exc

    async def embed(self, texts: list[str]) -> list[list[float]]:
        question_id = self._question_id() if self.observer is not None else None
        if not self._reserve_call():
            if self.observer is not None:
                self._emit(
                    "embedding.skipped",
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
            raise EmbeddingObservationError("embedding call limit reached")

        call_id = str(uuid4())
        request_payload = {
            "call_id": call_id,
            "question_id": question_id,
            "run_id": str(self.run_id) if self.run_id is not None else None,
            "model_id": self.model_id,
            "input_count": len(texts),
            "texts_sha256": canonical_json_sha256(texts),
            "call_state": "attempted",
        }
        self._emit("embedding.request", status="success", payload=request_payload, duration_ms=None)
        started = perf_counter()
        try:
            response = await self.provider.embed(texts)
            vector_count, dimensions = _validate_vectors(response, len(texts))
        except Exception as exc:
            duration_ms = (perf_counter() - started) * 1000
            try:
                self._emit(
                    "embedding.response",
                    status="failed",
                    payload={
                        "call_id": call_id,
                        "question_id": question_id,
                        "run_id": str(self.run_id) if self.run_id is not None else None,
                        "model_id": self.model_id,
                        "input_count": len(texts),
                        "call_state": "failed",
                        "error_code": _error_code(exc),
                    },
                    duration_ms=duration_ms,
                )
            except EmbeddingObservationError:
                # Preserve the provider/validation exception as the API error.
                pass
            raise

        self._emit(
            "embedding.response",
            status="success",
            payload={
                "call_id": call_id,
                "question_id": question_id,
                "run_id": str(self.run_id) if self.run_id is not None else None,
                "model_id": self.model_id,
                "vector_count": vector_count,
                "dimensions": dimensions,
                "response_sha256": canonical_json_sha256(deepcopy(response)),
                "call_state": "succeeded",
            },
            duration_ms=(perf_counter() - started) * 1000,
        )
        return response


__all__ = ["EmbeddingObservationError", "RecordingEmbeddingClient"]