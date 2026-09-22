import json

import pytest
from pydantic import ValidationError

from app.agent.models import LLMResponse
from app.knowledge.query_planning import (
    LLMQueryPlanner,
    QueryPlannerConfig,
    SafeQueryPlanner,
    is_complex_query,
)
from app.knowledge.pipeline_models import RetrievalQuery
from app.settings import Settings


class FakeLLM:
    def __init__(self, content: str | None, *, kind: str = "final"):
        self.response = LLMResponse(kind=kind, content=content)
        self.calls: list[tuple[list[object], list[object]]] = []

    async def complete(self, messages, tools):
        self.calls.append((messages, tools))
        return self.response


@pytest.mark.asyncio
async def test_safe_query_planner_keeps_a_simple_query_without_calling_a_model():
    planner = SafeQueryPlanner()

    plan = await planner.plan("SETEX 是什么？")

    assert plan.original_query == "SETEX 是什么？"
    assert [(query.query_id, query.text, query.facet) for query in plan.queries] == [
        ("q1", "SETEX 是什么？", "original")
    ]
    assert plan.is_multi_evidence is False


@pytest.mark.asyncio
async def test_safe_query_planner_rejects_empty_query():
    with pytest.raises(ValueError, match="empty"):
        await SafeQueryPlanner().plan("   ")


@pytest.mark.asyncio
async def test_llm_query_planner_preserves_original_and_adds_stable_subqueries():
    llm = FakeLLM(
        json.dumps(
            {
                "queries": [
                    {"text": "Q/K/V 各自承担什么职责？", "facet": "Q/K/V"},
                    {"text": "多头注意力如何使用独立子空间？", "facet": "多头注意力"},
                ]
            },
            ensure_ascii=False,
        )
    )
    planner = LLMQueryPlanner(QueryPlannerConfig(enabled=True))

    plan = await planner.plan("分别解释 Q/K/V 和多头注意力的关系？", llm_client=llm)

    assert plan.original_query == "分别解释 Q/K/V 和多头注意力的关系？"
    assert [(query.query_id, query.facet) for query in plan.queries] == [
        ("q1", "original"),
        ("q2", "Q/K/V"),
        ("q3", "多头注意力"),
    ]
    assert [query.text for query in plan.queries[1:]] == [
        "Q/K/V 各自承担什么职责？",
        "多头注意力如何使用独立子空间？",
    ]
    assert plan.is_multi_evidence is True
    assert len(llm.calls) == 1
    assert llm.calls[0][1] == []
    assert "citation" in llm.calls[0][0][0].content


@pytest.mark.asyncio
async def test_llm_query_planner_skips_model_for_simple_query():
    llm = FakeLLM('{"queries":[{"text":"不应调用","facet":"bad"}]}')
    planner = LLMQueryPlanner(QueryPlannerConfig(enabled=True))

    plan = await planner.plan("SETEX 是什么？", llm_client=llm)

    assert [query.text for query in plan.queries] == ["SETEX 是什么？"]
    assert llm.calls == []


@pytest.mark.asyncio
async def test_llm_query_planner_falls_back_when_disabled_or_without_model():
    disabled = LLMQueryPlanner(QueryPlannerConfig(enabled=False))
    without_model = LLMQueryPlanner(QueryPlannerConfig(enabled=True))

    disabled_plan = await disabled.plan("比较 Redis 和 Memcached", llm_client=FakeLLM("{}"))
    without_model_plan = await without_model.plan("比较 Redis 和 Memcached")

    assert [query.query_id for query in disabled_plan.queries] == ["q1"]
    assert [query.query_id for query in without_model_plan.queries] == ["q1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "```json\n{\"queries\": [{\"text\": \"Q/K/V\", \"facet\": \"qkv\"}]}\n```",
        '{"queries": [{"text": "Q/K/V", "facet": "qkv", "document_id": "forbidden"}]}',
        '{"queries": []}',
        json.dumps(
            {
                "queries": [
                    {"text": f"证据面-{index}", "facet": f"facet-{index}"}
                    for index in range(5)
                ]
            },
            ensure_ascii=False,
        ),
        '{"queries": [{"text": "超过长度"' + 'x' * 300 + ', "facet": "qkv"}]}',
        '{"queries": [{"text": "相同", "facet": "qkv"}, {"text": "相同", "facet": "other"}]}',
        '{"queries": [{"text": 123, "facet": "qkv"}]}',
        '{"queries": [{"text": "Q/K/V"}]}',
        '{"queries": [{"text": "Q/K/V", "facet": "qkv"}], "answer": "forbidden"}',
    ],
)
async def test_llm_query_planner_safely_falls_back_on_invalid_model_output(content: str):
    llm = FakeLLM(content)
    planner = LLMQueryPlanner(QueryPlannerConfig(enabled=True))

    plan = await planner.plan("分别解释 Q/K/V 和多头注意力", llm_client=llm)

    assert plan.original_query == "分别解释 Q/K/V 和多头注意力"
    assert plan.queries == (
        RetrievalQuery(
            query_id="q1",
            text="分别解释 Q/K/V 和多头注意力",
            facet="original",
        ),
    )
    assert plan.is_multi_evidence is False


@pytest.mark.asyncio
async def test_llm_query_planner_falls_back_on_provider_failure():
    class FailingLLM(FakeLLM):
        async def complete(self, messages, tools):
            raise RuntimeError("provider unavailable")

    planner = LLMQueryPlanner(QueryPlannerConfig(enabled=True))

    plan = await planner.plan("比较 Redis 和 Memcached", llm_client=FailingLLM(None))

    assert [query.query_id for query in plan.queries] == ["q1"]
    assert plan.queries[0].text == "比较 Redis 和 Memcached"


def test_complex_query_trigger_uses_explicit_relation_signals():
    assert is_complex_query("分别解释 Q/K/V 和多头注意力") is True
    assert is_complex_query("比较 Redis 和 Memcached 的过期策略") is True
    assert is_complex_query("SETEX 是什么？") is False


def test_query_planner_config_rejects_invalid_limits_and_settings_keep_baseline_defaults():
    with pytest.raises(ValidationError):
        QueryPlannerConfig(max_queries=0)
    with pytest.raises(ValidationError):
        QueryPlannerConfig(max_queries=5)
    with pytest.raises(ValidationError):
        QueryPlannerConfig(max_query_chars=0)

    settings = Settings()
    assert settings.knowledge_query_planning_enabled is False
    assert settings.knowledge_query_planning_max_queries == 4
    assert settings.knowledge_query_planning_max_query_chars == 300