import json

import pytest
import tiktoken

from app.knowledge.service import CitationSnapshot
from app.writing.execution_models import WritingExecutionError
from app.writing.outline import select_outline_context


def citation(citation_id: str, text: str) -> CitationSnapshot:
    return CitationSnapshot(
        citation_id=citation_id,
        chunk_id=citation_id,
        document_id="doc-1",
        document_version="v1",
        source_path="redis.md",
        source_url=None,
        title="Redis",
        heading_path="段落",
        start_line=1,
        end_line=1,
        text=text,
    )


def test_context_drops_whole_trailing_evidence_blocks_and_returns_sent_set():
    first = citation("chunk-1", "第一段证据")
    second = citation("chunk-2", "第二段证据")
    full_messages, _ = select_outline_context("Redis", [first, second], max_context_tokens=8000)
    one_messages, _ = select_outline_context("Redis", [first], max_context_tokens=8000)
    encoding = tiktoken.get_encoding("cl100k_base")
    budget = len(encoding.encode(json.dumps(
        [message.model_dump(mode="json") for message in one_messages],
        ensure_ascii=False,
        sort_keys=True,
    )))

    messages, sent = select_outline_context(
        "Redis", [first, second], max_context_tokens=max(1, budget)
    )

    assert sent == [first]
    assert "第一段证据" in messages[-1].content
    assert "第二段证据" not in messages[-1].content
    assert len(full_messages[-1].content) > len(one_messages[-1].content)


def test_context_fails_when_required_messages_cannot_fit():
    with pytest.raises(WritingExecutionError) as error:
        select_outline_context("Redis", [citation("chunk-1", "证据")], max_context_tokens=1)
    assert error.value.code == "context_budget_exceeded"