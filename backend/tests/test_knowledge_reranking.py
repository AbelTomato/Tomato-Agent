import json

import httpx
import pytest
from pydantic import ValidationError

from app.knowledge.models import SearchResult
from app.knowledge.pipeline_models import CandidateEvidence
from app.knowledge.reranking import (
    CompatibleReranker,
    NoopReranker,
    RerankerError,
)
from app.settings import Settings


def candidate(chunk_id: str, score: float = 0.5) -> CandidateEvidence:
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
            text=f"候选正文 {chunk_id}",
            score=score,
        ),
        query_ids=("q1",),
        retrieval_ranks=(1,),
        retrieval_score_type="cosine",
    )


@pytest.mark.asyncio
async def test_noop_reranker_preserves_stable_order_and_does_not_overwrite_retrieval_score():
    candidates = [candidate("a", 0.9), candidate("b", 0.8)]

    ranked = await NoopReranker().rank("问题", candidates)

    assert [item.result.chunk_id for item in ranked] == ["a", "b"]
    assert [item.rerank_score for item in ranked] == [None, None]
    assert [item.result.score for item in ranked] == [0.9, 0.8]
    assert ranked is not candidates


@pytest.mark.asyncio
async def test_compatible_reranker_can_promote_seventh_candidate_and_sorts_stably():
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 6, "score": 0.9},
                    {"index": 0, "score": 0.9},
                    *[{"index": index, "score": 0.1} for index in range(1, 6)],
                ]
            },
            request=request,
        )

    candidates = [candidate(str(index), 0.5) for index in range(7)]
    reranker = CompatibleReranker(
        api_key="test-key",
        base_url="https://rerank.example/v1",
        model="rerank-model",
        transport=httpx.MockTransport(handler),
    )

    ranked = await reranker.rank("原始问题", candidates)

    assert [item.result.chunk_id for item in ranked[:2]] == ["0", "6"]
    assert [item.rerank_score for item in ranked[:2]] == [0.9, 0.9]
    assert [item.result.score for item in ranked] == [0.5] * 7
    assert captured["path"] == "/v1/rerank"
    assert captured["payload"] == {
        "model": "rerank-model",
        "query": "原始问题",
        "candidates": [
            {"index": index, "text": f"候选正文 {index}"} for index in range(7)
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "results",
    [
        [],
        [{"index": 0, "score": 0.5}],
        [{"index": 0, "score": 0.5}, {"index": 0, "score": 0.4}],
        [{"index": 99, "score": 0.5}, {"index": 1, "score": 0.4}],
        [{"index": 0, "score": float("nan")}, {"index": 1, "score": 0.4}],
        [{"index": 0, "score": -0.1}, {"index": 1, "score": 0.4}],
    ],
)
async def test_compatible_reranker_rejects_invalid_result_shape(results):
    async def handler(request: httpx.Request) -> httpx.Response:
        if any(item.get("score") != item.get("score") for item in results):
            return httpx.Response(
                200,
                content=b'{"results":[{"index":0,"score":NaN},{"index":1,"score":0.4}]}',
                headers={"Content-Type": "application/json"},
                request=request,
            )
        return httpx.Response(200, json={"results": results}, request=request)

    reranker = CompatibleReranker(
        api_key="test-key",
        base_url="https://rerank.example/v1",
        model="rerank-model",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RerankerError, match="response"):
        await reranker.rank("问题", [candidate("a"), candidate("b")])


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 429, 500])
async def test_compatible_reranker_propagates_http_failures_as_errors(status_code: int):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "secret"}, request=request)

    reranker = CompatibleReranker(
        api_key="secret",
        base_url="https://rerank.example/v1",
        model="rerank-model",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RerankerError, match=f"HTTP {status_code}") as error:
        await reranker.rank("问题", [candidate("a")])
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_compatible_reranker_converts_timeout_and_transport_errors():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    reranker = CompatibleReranker(
        api_key="test-key",
        base_url="https://rerank.example/v1",
        model="rerank-model",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RerankerError, match="request failed"):
        await reranker.rank("问题", [candidate("a")])


def test_reranker_settings_have_safe_disabled_defaults_and_validate_limits():
    settings = Settings()

    assert settings.knowledge_rerank_enabled is False
    assert settings.knowledge_rerank_base_url == ""
    assert settings.knowledge_rerank_model == ""
    assert settings.knowledge_rerank_timeout_seconds == 10.0
    assert settings.knowledge_rerank_candidate_limit == 30

    with pytest.raises(ValidationError):
        Settings(knowledge_rerank_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(knowledge_rerank_candidate_limit=0)