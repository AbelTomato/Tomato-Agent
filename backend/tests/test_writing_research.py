import pytest

from app.agent.models import LLMResponse
from app.knowledge.service import CitationSnapshot
from app.writing.execution_models import ExecutionConfig, ResearchBundle, WritingExecutionError
from app.writing.outline import OutlineGenerator
from app.writing.research import WritingResearcher


def citation(citation_id: str = "chunk-1", *, text: str = "证据正文") -> CitationSnapshot:
    return CitationSnapshot(
        citation_id=citation_id,
        chunk_id=citation_id,
        document_id="doc-1",
        document_version="v1",
        source_path="redis.md",
        source_url=None,
        title="Redis",
        heading_path="过期",
        start_line=1,
        end_line=1,
        text=text,
    )


class FakeKnowledgeService:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def retrieve(self, query, *, mode, limit):
        self.calls.append((query, mode, limit))
        return self.answer

    async def answer(self, *args, **kwargs):
        raise AssertionError("研究阶段不得调用 answer()")


@pytest.mark.asyncio
async def test_research_collect_forwards_mode_limit_and_copies_snapshots():
    original = citation()
    service = FakeKnowledgeService(
        type("Answer", (), {
            "evidence_status": "supported",
            "retrieval_mode": "vector",
            "citations": [original],
        })()
    )

    bundle = await WritingResearcher(service).collect(
        "Redis", config=ExecutionConfig(retrieval_mode="vector", max_evidence=3)
    )

    assert service.calls == [("Redis", "vector", 3)]
    assert bundle.citations == [original]
    assert bundle.citations[0] is not original
    assert bundle.citations[0].text == "证据正文"


class ForbiddenLLM:
    async def complete(self, messages, tools):
        raise AssertionError("没有证据时不能调用模型")


@pytest.mark.asyncio
async def test_no_evidence_skips_outline_model():
    generator = OutlineGenerator(ForbiddenLLM())
    bundle = ResearchBundle(
        evidence_status="no_results", citations=[], retrieval_mode="keyword",
    )
    with pytest.raises(WritingExecutionError) as error:
        await generator.generate("未知主题", bundle, config=ExecutionConfig())
    assert error.value.code == "evidence_insufficient"


class RecordingLLM:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def complete(self, messages, tools):
        self.calls.append((messages, tools))
        return self.response


@pytest.mark.asyncio
async def test_outline_generation_calls_provider_once_without_tools():
    llm = RecordingLLM(LLMResponse(kind="final", content='{"title":"Redis"}'))
    generator = OutlineGenerator(llm)
    bundle = ResearchBundle(
        evidence_status="supported", citations=[citation()], retrieval_mode="keyword",
    )

    raw, sent = await generator.generate("Redis", bundle, config=ExecutionConfig())

    assert raw == '{"title":"Redis"}'
    assert sent == bundle.citations
    assert len(llm.calls) == 1
    messages, tools = llm.calls[0]
    assert tools == []
    assert all(message.role != "assistant" for message in messages)
    assert "Redis" in messages[-1].content
    assert "证据正文" in messages[-1].content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [LLMResponse(kind="tool_call"), LLMResponse(kind="clarification"), LLMResponse(kind="final", content="")],
)
async def test_outline_generation_rejects_non_final_or_empty_response(response):
    bundle = ResearchBundle(
        evidence_status="supported", citations=[citation()], retrieval_mode="keyword",
    )
    with pytest.raises(WritingExecutionError) as error:
        await OutlineGenerator(RecordingLLM(response)).generate(
            "Redis", bundle, config=ExecutionConfig()
        )
    assert error.value.code == "invalid_model_response"


@pytest.mark.asyncio
async def test_outline_generation_maps_provider_exception():
    class FailingLLM:
        async def complete(self, messages, tools):
            raise RuntimeError("provider details must not escape")

    bundle = ResearchBundle(
        evidence_status="supported", citations=[citation()], retrieval_mode="keyword",
    )
    with pytest.raises(WritingExecutionError) as error:
        await OutlineGenerator(FailingLLM()).generate(
            "Redis", bundle, config=ExecutionConfig()
        )
    assert error.value.code == "provider_failed"