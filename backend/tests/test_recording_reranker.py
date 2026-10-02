from __future__ import annotations

from uuid import uuid4

import pytest

from app.knowledge.models import SearchResult
from app.knowledge.pipeline_models import CandidateEvidence
from app.observability.recording_llm_client import llm_question_scope
from app.observability.recording_reranker import RecordingReranker, RerankerObservationError


class EventCollector:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def emit(self, event_type, *, status, payload, duration_ms) -> None:
        self.events.append((event_type, status, payload))


class FakeReranker:
    def __init__(self, responses: list[list[CandidateEvidence] | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, tuple[CandidateEvidence, ...]]] = []

    async def rank(self, query, candidates):
        self.calls.append((query, tuple(candidates)))
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, Exception):
            raise response
        return response


def candidate(chunk_id: str) -> CandidateEvidence:
    return CandidateEvidence(
        result=SearchResult(
            chunk_id=chunk_id,
            document_id=f"doc-{chunk_id}",
            document_version="v1",
            source_path=f"{chunk_id}.md",
            source_url=None,
            title=chunk_id,
            heading_path="正文",
            start_line=1,
            end_line=2,
            text=f"证据 {chunk_id}",
            score=0.5,
        ),
        query_ids=("q1",),
        retrieval_ranks=(1,),
        retrieval_score_type="bm25",
    )


@pytest.mark.asyncio
async def test_recording_reranker_records_success_and_budget_skip():
    collector = EventCollector()
    item = candidate("one")
    provider = FakeReranker([[item]])
    client = RecordingReranker(
        provider,
        observer=collector,
        run_id=uuid4(),
        model_id="offline-reranker",
        max_calls=1,
    )

    with llm_question_scope("blog-dev-reranker"):
        assert await client.rank("问题", [item]) == [item]
        with pytest.raises(RerankerObservationError, match="call limit"):
            await client.rank("问题", [item])

    assert len(provider.calls) == 1
    assert [(event_type, status) for event_type, status, _ in collector.events] == [
        ("reranker.request", "success"),
        ("reranker.response", "success"),
        ("reranker.skipped", "skipped"),
    ]
    assert collector.events[0][2]["call_state"] == "attempted"
    assert collector.events[1][2]["call_state"] == "succeeded"
    assert collector.events[2][2]["call_state"] == "skipped"


@pytest.mark.asyncio
async def test_recording_reranker_records_invalid_provider_response_as_failure():
    collector = EventCollector()
    provider_error = ValueError("incomplete rerank response")
    client = RecordingReranker(
        FakeReranker([provider_error]),
        observer=collector,
        run_id=uuid4(),
        model_id="offline-reranker",
    )

    with llm_question_scope("blog-dev-reranker-failure"):
        with pytest.raises(ValueError) as caught:
            await client.rank("问题", [candidate("one")])

    assert caught.value is provider_error
    assert collector.events[-1][0:2] == ("reranker.response", "failed")
    assert collector.events[-1][2]["call_state"] == "failed"


def test_recording_reranker_rejects_non_positive_budget():
    with pytest.raises(ValueError, match="max_calls"):
        RecordingReranker(FakeReranker([]), max_calls=0)