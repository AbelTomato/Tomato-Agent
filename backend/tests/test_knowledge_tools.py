from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.agent.config import RuntimeConfig
from app.agent.context import ContextManager
from app.agent.models import LLMResponse, ToolCall
from app.agent.runtime import AgentRuntime
from app.knowledge.ingestion import ingest_manifest
from app.knowledge.repository import KnowledgeRepository
from app.sessions.repository import SessionRepository
from app.tools.base import ToolContext
from app.tools.read_knowledge import ReadKnowledge
from app.tools.registry import ToolRegistry
from app.tools.search_knowledge import SearchKnowledge


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "knowledge"


class FakeLLM:
    def __init__(self, *responses: LLMResponse):
        self.responses = list(responses)
        self.messages = []

    async def complete(self, messages, tools):
        self.messages.append((messages, tools))
        return self.responses.pop(0)


async def make_knowledge_repository(tmp_path: Path) -> KnowledgeRepository:
    repository = KnowledgeRepository(tmp_path / "knowledge.db")
    await repository.init()
    report = await ingest_manifest(
        repository,
        FIXTURE_ROOT / "manifest.json",
        FIXTURE_ROOT,
    )
    assert report.failed == 0
    return repository


@pytest.mark.asyncio
async def test_search_knowledge_returns_bounded_citations_and_definition(tmp_path):
    repository = await make_knowledge_repository(tmp_path)
    tool = SearchKnowledge(repository, max_result_chars=80)

    result = await tool.execute(
        {"query": "SETEX 过期时间", "top_k": 2},
        ToolContext(session_id="s", run_id="r"),
    )

    assert result.success
    assert len(result.data["results"]) == 1
    item = result.data["results"][0]
    assert item["chunk_id"]
    assert item["document_version"]
    assert item["start_line"] <= item["end_line"]
    assert len(item["text"]) <= 80
    assert tool.definition().name == "search_knowledge"
    assert "top_k" in tool.definition().parameters["properties"]


@pytest.mark.asyncio
async def test_search_knowledge_validates_query_and_top_k(tmp_path):
    repository = await make_knowledge_repository(tmp_path)
    tool = SearchKnowledge(repository)

    with pytest.raises(ValidationError):
        await tool.execute({"query": "", "top_k": 1}, ToolContext(session_id="s", run_id="r"))
    with pytest.raises(ValidationError):
        await tool.execute({"query": "Redis", "top_k": 11}, ToolContext(session_id="s", run_id="r"))


@pytest.mark.asyncio
async def test_read_knowledge_uses_chunk_id_not_path_and_limits_text(tmp_path):
    repository = await make_knowledge_repository(tmp_path)
    chunk = (await repository.list_chunks())[0]
    tool = ReadKnowledge(repository, max_result_chars=20)
    context = ToolContext(session_id="s", run_id="r")

    result = await tool.execute({"chunk_id": chunk.chunk_id}, context)

    assert result.success
    assert result.data["chunk_id"] == chunk.chunk_id
    assert result.data["document_version"] == chunk.document_version
    assert len(result.data["text"]) <= 20
    assert chunk.chunk_id in context.read_chunk_ids
    unknown = await tool.execute({"chunk_id": "../../secret.md"}, context)
    assert not unknown.success
    assert "not found" in unknown.error.lower()


@pytest.mark.asyncio
async def test_runtime_records_search_then_read_trace_and_final_answer(tmp_path):
    repository = await make_knowledge_repository(tmp_path)
    chunks = await repository.list_chunks()
    target = next(chunk for chunk in chunks if "SETEX" in chunk.text)
    llm = FakeLLM(
        LLMResponse(
            kind="tool_call",
            tool_call=ToolCall(name="search_knowledge", arguments={"query": "SETEX"}),
        ),
        LLMResponse(
            kind="tool_call",
            tool_call=ToolCall(name="read_knowledge", arguments={"chunk_id": target.chunk_id}),
        ),
        LLMResponse(kind="final", content="SETEX 会同时设置值和过期时间。"),
    )
    sessions = SessionRepository(tmp_path / "runtime.db")
    await sessions.init()
    session_id = await sessions.create_session()
    runtime = AgentRuntime(
        llm=llm,
        context_manager=ContextManager(token_counter=len),
        tool_registry=ToolRegistry([
            SearchKnowledge(repository),
            ReadKnowledge(repository),
        ]),
        repository=sessions,
        system_instruction="Only cite knowledge chunks returned by read_knowledge.",
    )

    result = await runtime.run(session_id, "SETEX 是什么？")

    assert result.status == "completed"
    assert result.answer == "SETEX 会同时设置值和过期时间。"
    events = await sessions.list_events(result.run_id)
    tool_events = [event for event in events if event.event_type == "tool_result"]
    assert [event.payload["tool_name"] for event in tool_events] == [
        "search_knowledge",
        "read_knowledge",
    ]
    assert target.chunk_id in tool_events[1].payload["read_chunk_ids"]
    assert target.document_version in tool_events[1].payload["read_document_versions"]


@pytest.mark.asyncio
async def test_runtime_passes_tool_failure_to_llm_and_stops_when_budget_exhausted(tmp_path):
    repository = await make_knowledge_repository(tmp_path)
    sessions = SessionRepository(tmp_path / "runtime.db")
    await sessions.init()
    session_id = await sessions.create_session()
    llm = FakeLLM(
        LLMResponse(
            kind="tool_call",
            tool_call=ToolCall(name="read_knowledge", arguments={"chunk_id": "missing"}),
        ),
        LLMResponse(kind="final", content="材料不足，无法回答。"),
    )
    runtime = AgentRuntime(
        llm=llm,
        context_manager=ContextManager(token_counter=len),
        tool_registry=ToolRegistry([ReadKnowledge(repository)]),
        repository=sessions,
        system_instruction="Do not invent evidence.",
        config=RuntimeConfig(max_tool_calls=1),
    )

    result = await runtime.run(session_id, "读取不存在的片段")

    assert result.status == "failed"
    assert result.error["message"] == "Maximum tool call count exceeded"
    assert len(llm.messages) == 1
    events = await sessions.list_events(result.run_id)
    tool_event = next(event for event in events if event.event_type == "tool_result")
    assert tool_event.payload["success"] is False
    assert "not found" in tool_event.payload["content"]