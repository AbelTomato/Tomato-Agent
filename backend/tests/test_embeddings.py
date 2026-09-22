import math

import httpx
import pytest

from app.knowledge.embeddings import EmbeddingClient, EmbeddingError
from app.settings import Settings


def transport_with_json(status_code: int, payload: object) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(status_code, json=payload, request=request)
    )


def test_embedding_settings_are_independent_from_chat_settings():
    settings = Settings(
        llm_api_key="chat-key",
        llm_model="chat-model",
        embedding_api_key="embedding-key",
        embedding_base_url="https://embedding.example/v1",
        embedding_model="embedding-model",
        embedding_dimensions=1024,
    )

    assert settings.embedding_api_key == "embedding-key"
    assert settings.embedding_base_url == "https://embedding.example/v1"
    assert settings.embedding_model == "embedding-model"
    assert settings.embedding_dimensions == 1024


@pytest.mark.asyncio
async def test_embedding_client_restores_batch_input_order():
    client = EmbeddingClient(
        api_key="test-secret",
        base_url="https://embedding.example/v1",
        model="test-embedding",
        dimensions=3,
        transport=transport_with_json(
            200,
            {
                "data": [
                    {"index": 1, "embedding": [4, 5, 6]},
                    {"index": 0, "embedding": [1, 2, 3]},
                ]
            },
        ),
    )

    vectors = await client.embed(["first", "second"])

    assert vectors == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]


@pytest.mark.asyncio
async def test_embedding_client_reports_http_failure_without_credential():
    client = EmbeddingClient(
        api_key="test-secret",
        base_url="https://embedding.example/v1",
        model="test-embedding",
        dimensions=3,
        transport=transport_with_json(503, {"error": {"message": "test-secret"}}),
    )

    with pytest.raises(EmbeddingError, match="HTTP 503") as error:
        await client.embed(["text"])

    assert "test-secret" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "raw_content"),
    [
        ({"data": [{"index": 0, "embedding": []}]}, None),
        ({"data": [{"index": 0, "embedding": [1, 2]}]}, None),
        (None, b'{"data":[{"index":0,"embedding":[1,NaN,3]}]}'),
        (None, b'{"data":[{"index":0,"embedding":[1,Infinity,3]}]}'),
    ],
)
async def test_embedding_client_rejects_invalid_vectors(
    payload: object, raw_content: bytes | None
):
    if raw_content is None:
        transport = transport_with_json(200, payload)
    else:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=raw_content,
                headers={"Content-Type": "application/json"},
                request=request,
            )
        )
    client = EmbeddingClient(
        api_key="test-secret",
        base_url="https://embedding.example/v1",
        model="test-embedding",
        dimensions=3,
        transport=transport,
    )

    with pytest.raises(EmbeddingError, match="(?i)embedding"):
        await client.embed(["text"])


@pytest.mark.asyncio
async def test_embedding_client_rejects_missing_or_duplicate_batch_indices():
    client = EmbeddingClient(
        api_key="test-secret",
        base_url="https://embedding.example/v1",
        model="test-embedding",
        dimensions=3,
        transport=transport_with_json(
            200,
            {
                "data": [
                    {"index": 0, "embedding": [1, 2, 3]},
                    {"index": 0, "embedding": [4, 5, 6]},
                ]
            },
        ),
    )

    with pytest.raises(EmbeddingError, match="indices"):
        await client.embed(["first", "second"])


@pytest.mark.asyncio
async def test_embedding_client_does_not_request_provider_for_empty_input():
    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    client = EmbeddingClient(
        api_key="test-secret",
        base_url="https://embedding.example/v1",
        model="test-embedding",
        dimensions=3,
        transport=httpx.MockTransport(unexpected_request),
    )

    assert await client.embed([]) == []