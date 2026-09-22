import httpx
import pytest
from app.agent.models import LLMResponse
from app.knowledge.models import Chunk, Document

import app.main as main
from app.knowledge.models import SearchResult


class ThresholdKnowledgeRepository:
    def __init__(self, result: SearchResult):
        self.result = result

    async def search_chunks(self, query: str, limit: int = 5):
        return [self.result][:limit]

    async def search_vector_chunks(
        self,
        query_vector,
        *,
        model: str,
        dimensions: int,
        limit: int = 5,
        min_score: float | None = None,
    ):
        if min_score is not None and self.result.score < min_score:
            return []
        return [self.result][:limit]

    async def search_hybrid_chunks(
        self,
        query: str,
        query_vector,
        *,
        model: str,
        dimensions: int,
        limit: int = 5,
        min_vector_score: float | None = None,
    ):
        if min_vector_score is not None and self.result.score < min_vector_score:
            return []
        return [self.result][:limit]


class RecordingLLM:
    def __init__(self, response: LLMResponse):
        self.response = response
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        return self.response


class FakeKnowledgeService:
    def __init__(self, answer):
        self.result = answer
        self.calls: list[tuple[str, str, int]] = []

    async def answer(self, query: str, *, mode: str, limit: int, llm_client):
        self.calls.append((query, mode, limit))
        return self.result


class FollowUpKnowledgeService(FakeKnowledgeService):
    def __init__(self, answer):
        super().__init__(answer)
        self.rewrite_calls: list[tuple[str, list[dict[str, object]]]] = []

    async def rewrite_query(self, query: str, history, *, llm_client):
        self.rewrite_calls.append((query, history))
        return "Redis SETEX 过期时间"

    async def answer(
        self,
        query: str,
        *,
        mode: str,
        limit: int,
        llm_client,
        retrieval_query: str | None = None,
    ):
        self.calls.append((retrieval_query or query, mode, limit))
        return self.result


@pytest.mark.asyncio
async def test_knowledge_run_returns_structured_answer_and_citations(monkeypatch, tmp_path):
    repository = main.SessionRepository(tmp_path / "api.db")
    await repository.init()
    session_id = await repository.create_session()
    knowledge_repository = main.KnowledgeRepository(tmp_path / "knowledge.db")
    await knowledge_repository.init()
    answer = main.KnowledgeAnswer(
        answer="SETEX 会设置过期时间。",
        retrieval_mode="keyword",
        evidence_status="supported",
        citations=[
            main.CitationSnapshot.from_search_result(
                SearchResult(
                    chunk_id="chunk-1",
                    document_id="doc-1",
                    document_version="v1",
                    source_path="redis.md",
                    source_url="https://example.test/redis",
                    title="Redis",
                    heading_path="Redis > SETEX",
                    start_line=3,
                    end_line=5,
                    text="SETEX 设置过期时间。",
                    score=1.0,
                )
            )
        ],
    )
    service = FakeKnowledgeService(answer)
    monkeypatch.setattr(main, "repo", repository)
    monkeypatch.setattr(main, "knowledge_repository", knowledge_repository)
    monkeypatch.setattr(main, "knowledge_service", service)

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/sessions/{session_id}/knowledge-runs",
            json={"message": "SETEX 做什么？", "retrieval_mode": "keyword", "limit": 1},
        )

    assert response.status_code == 200
    assert response.json()["answer"] == "SETEX 会设置过期时间。"
    assert response.json()["citations"][0]["citation_id"] == "chunk-1"
    assert response.json()["citations"][0]["source_path"] == "redis.md"
    assert response.json()["citations"][0]["heading_path"] == "Redis > SETEX"
    assert response.json()["citations"][0]["text"] == "SETEX 设置过期时间。"
    assert response.json()["evidence_status"] == "supported"
    assert response.json()["session_id"] == str(session_id)
    assert response.json()["status"] == "completed"
    assert response.json()["run_id"]
    assert service.calls == [("SETEX 做什么？", "keyword", 1)]

    events = await repository.list_session_events(session_id)
    assert [event.event_type for event in events] == ["user_message", "assistant_message"]
    assert events[0].payload["content"] == "SETEX 做什么？"
    assert events[1].payload["citations"][0]["citation_id"] == "chunk-1"


@pytest.mark.asyncio
async def test_knowledge_run_returns_no_results_without_citations_when_vector_score_is_low(
    monkeypatch, tmp_path
):
    repository = main.SessionRepository(tmp_path / "api.db")
    await repository.init()
    session_id = await repository.create_session()
    result = SearchResult(
        chunk_id="chunk-low",
        document_id="doc-1",
        document_version="v1",
        source_path="unrelated.md",
        source_url=None,
        title="无关内容",
        heading_path="",
        start_line=1,
        end_line=2,
        text="与问题无关。",
        score=0.2,
    )
    async def embedder(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0]]

    llm = RecordingLLM(
        LLMResponse(
            kind="final",
            content='{"answer":"不应生成","citation_ids":["chunk-low"],"evidence_status":"supported"}',
        )
    )
    service = main.KnowledgeService(
        ThresholdKnowledgeRepository(result),
        query_embedder=embedder,
        embedding_model="test-model",
        embedding_dimensions=2,
        min_vector_similarity=0.5,
    )
    monkeypatch.setattr(main, "repo", repository)
    monkeypatch.setattr(main, "knowledge_service", service)
    monkeypatch.setattr(main, "llm_client", llm)

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/sessions/{session_id}/knowledge-runs",
            json={"message": "知识库没有的问题", "retrieval_mode": "vector", "limit": 1},
        )

    assert response.status_code == 200
    assert response.json()["evidence_status"] == "no_results"
    assert response.json()["citations"] == []
    assert response.json()["answer"] == ""
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_knowledge_run_keeps_citation_when_vector_score_is_above_threshold(
    monkeypatch, tmp_path
):
    repository = main.SessionRepository(tmp_path / "api.db")
    await repository.init()
    session_id = await repository.create_session()
    result = SearchResult(
        chunk_id="chunk-high",
        document_id="doc-1",
        document_version="v1",
        source_path="redis.md",
        source_url="https://example.test/redis",
        title="Redis",
        heading_path="Redis > SETEX",
        start_line=3,
        end_line=5,
        text="SETEX 设置过期时间。",
        score=0.8,
    )
    async def embedder(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0]]

    llm = RecordingLLM(
        LLMResponse(
            kind="final",
            content='{"answer":"SETEX 设置过期时间。","citation_ids":["chunk-high"],"evidence_status":"supported"}',
        )
    )
    service = main.KnowledgeService(
        ThresholdKnowledgeRepository(result),
        query_embedder=embedder,
        embedding_model="test-model",
        embedding_dimensions=2,
        min_vector_similarity=0.5,
    )
    monkeypatch.setattr(main, "repo", repository)
    monkeypatch.setattr(main, "knowledge_service", service)
    monkeypatch.setattr(main, "llm_client", llm)

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/sessions/{session_id}/knowledge-runs",
            json={"message": "SETEX 做什么？", "retrieval_mode": "vector", "limit": 1},
        )

    assert response.status_code == 200
    assert response.json()["evidence_status"] == "supported"
    assert [item["citation_id"] for item in response.json()["citations"]] == ["chunk-high"]
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_messages_endpoint_returns_completed_knowledge_turn(tmp_path, monkeypatch):
    repository = main.SessionRepository(tmp_path / "api.db")
    await repository.init()
    session_id = await repository.create_session()
    knowledge_repository = main.KnowledgeRepository(tmp_path / "knowledge.db")
    await knowledge_repository.init()
    answer = main.KnowledgeAnswer(
        answer="材料不足。",
        retrieval_mode="keyword",
        evidence_status="no_results",
        citations=[],
    )
    monkeypatch.setattr(main, "repo", repository)
    monkeypatch.setattr(main, "knowledge_repository", knowledge_repository)
    monkeypatch.setattr(main, "knowledge_service", FakeKnowledgeService(answer))

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            f"/api/sessions/{session_id}/knowledge-runs",
            json={"message": "未知问题"},
        )
        messages = await client.get(f"/api/sessions/{session_id}/messages")

    assert created.status_code == 200
    assert messages.status_code == 200
    assert [item["role"] for item in messages.json()["messages"]] == ["user", "assistant"]
    assert messages.json()["messages"][0]["content"] == "未知问题"
    assert messages.json()["messages"][1]["evidence_status"] == "no_results"


@pytest.mark.asyncio
async def test_document_endpoint_resolves_only_known_document_id(tmp_path, monkeypatch):
    session_repository = main.SessionRepository(tmp_path / "api.db")
    await session_repository.init()
    knowledge_repository = main.KnowledgeRepository(tmp_path / "knowledge.db")
    await knowledge_repository.init()
    document = Document("doc-1", "redis.md", "https://example.test/redis", "Redis", "v1")
    chunk = Chunk("chunk-1", "doc-1", "v1", "Redis > SETEX", 3, 5, "SETEX", 1)
    await knowledge_repository.replace_document(document, [chunk])
    monkeypatch.setattr(main, "knowledge_repository", knowledge_repository)

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        found = await client.get("/api/knowledge/documents/doc-1")
        missing = await client.get("/api/knowledge/documents/redis.md")

    assert found.status_code == 200
    assert found.json()["document"]["document_id"] == "doc-1"
    assert found.json()["chunks"][0]["start_line"] == 3
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_knowledge_run_rejects_unknown_session_and_empty_message(tmp_path):
    repository = main.SessionRepository(tmp_path / "api.db")
    await repository.init()
    import app.main as main_module

    original = main_module.repo
    main_module.repo = repository
    try:
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            unknown = await client.post(
                "/api/sessions/00000000-0000-0000-0000-000000000000/knowledge-runs",
                json={"message": "query"},
            )
            empty = await client.post(
                "/api/sessions/not-a-uuid/knowledge-runs",
                json={"message": "   "},
            )
    finally:
        main_module.repo = original

    assert unknown.status_code == 404
    assert empty.status_code == 400


@pytest.mark.asyncio
async def test_knowledge_follow_up_rewrites_query_and_preserves_previous_citation_snapshot(
    tmp_path, monkeypatch
):
    repository = main.SessionRepository(tmp_path / "api.db")
    await repository.init()
    session_id = await repository.create_session()
    knowledge_repository = main.KnowledgeRepository(tmp_path / "knowledge.db")
    await knowledge_repository.init()
    answer = main.KnowledgeAnswer(
        answer="SETEX 会设置过期时间。",
        retrieval_mode="keyword",
        evidence_status="supported",
        citations=[
            main.CitationSnapshot.from_search_result(
                SearchResult(
                    chunk_id="chunk-1",
                    document_id="doc-1",
                    document_version="v1",
                    source_path="redis.md",
                    source_url="https://example.test/redis",
                    title="Redis",
                    heading_path="Redis > SETEX",
                    start_line=3,
                    end_line=5,
                    text="SETEX 设置过期时间。",
                    score=1.0,
                )
            )
        ],
    )
    service = FollowUpKnowledgeService(answer)
    monkeypatch.setattr(main, "repo", repository)
    monkeypatch.setattr(main, "knowledge_repository", knowledge_repository)
    monkeypatch.setattr(main, "knowledge_service", service)

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post(
            f"/api/sessions/{session_id}/knowledge-runs",
            json={"message": "Redis 的 SETEX 是什么？"},
        )
        second = await client.post(
            f"/api/sessions/{session_id}/knowledge-runs",
            json={"message": "它多久过期？"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert service.rewrite_calls[0][0] == "它多久过期？"
    assert [item["content"] for item in service.rewrite_calls[0][1]] == [
        "Redis 的 SETEX 是什么？",
        "SETEX 会设置过期时间。",
    ]
    assert service.calls[-1] == ("Redis SETEX 过期时间", "keyword", 5)

    events = await repository.list_session_events(session_id)
    assistant_events = [event for event in events if event.event_type == "assistant_message"]
    assert assistant_events[-1].payload["citations"][0]["document_version"] == "v1"
    user_events = [event for event in events if event.event_type == "user_message"]
    assert user_events[-1].payload["original_query"] == "它多久过期？"
    assert user_events[-1].payload["retrieval_query"] == "Redis SETEX 过期时间"