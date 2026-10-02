from __future__ import annotations

from uuid import uuid4

import pytest

from app.observability.recording_embedding_client import (
    EmbeddingObservationError,
    RecordingEmbeddingClient,
)
from app.observability.recording_llm_client import llm_question_scope


class EventCollector:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def emit(self, event_type, *, status, payload, duration_ms) -> None:
        self.events.append((event_type, status, payload))


class FakeEmbedding:
    def __init__(self, responses: list[list[list[float]] | Exception]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.asyncio
async def test_recording_embedding_client_records_success_failure_and_budget_skip():
    collector = EventCollector()
    provider_error = RuntimeError("provider diagnostic must not be persisted")
    provider = FakeEmbedding([[[1.0, 0.0]], provider_error])
    client = RecordingEmbeddingClient(
        provider,
        observer=collector,
        run_id=uuid4(),
        model_id="offline-embedding",
        max_calls=1,
    )

    with llm_question_scope("blog-dev-embedding"):
        assert await client.embed(["first"]) == [[1.0, 0.0]]
        with pytest.raises(EmbeddingObservationError, match="call limit"):
            await client.embed(["second"])

    assert provider.calls == [["first"]]
    assert [(event_type, status) for event_type, status, _ in collector.events] == [
        ("embedding.request", "success"),
        ("embedding.response", "success"),
        ("embedding.skipped", "skipped"),
    ]
    assert collector.events[0][2]["call_state"] == "attempted"
    assert collector.events[1][2]["call_state"] == "succeeded"
    assert collector.events[2][2]["call_state"] == "skipped"
    assert collector.events[2][2]["reason"] == "budget_exceeded"
    assert "provider diagnostic" not in repr(collector.events)


@pytest.mark.asyncio
async def test_recording_embedding_client_records_provider_failure_and_reraises_it():
    collector = EventCollector()
    provider_error = RuntimeError("private provider diagnostic")
    provider = FakeEmbedding([provider_error])
    client = RecordingEmbeddingClient(
        provider,
        observer=collector,
        run_id=uuid4(),
        model_id="offline-embedding",
    )

    with llm_question_scope("blog-dev-embedding-failure"):
        with pytest.raises(RuntimeError) as caught:
            await client.embed(["first"])

    assert caught.value is provider_error
    assert collector.events[-1][0:2] == ("embedding.response", "failed")
    assert collector.events[-1][2]["call_state"] == "failed"
    assert "private provider diagnostic" not in repr(collector.events)


def test_recording_embedding_client_rejects_non_positive_budget():
    with pytest.raises(ValueError, match="max_calls"):
        RecordingEmbeddingClient(FakeEmbedding([]), max_calls=0)