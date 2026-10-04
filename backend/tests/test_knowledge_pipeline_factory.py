import pytest
from pydantic import ValidationError

from app.knowledge.answerability import CoverageAnswerabilityJudge
from app.knowledge.candidate_retrieval import RepositoryCandidateRetriever
from app.knowledge.embeddings import EmbeddingClient
from app.knowledge.evidence_selection import CoverageAwareEvidenceSelector
from app.knowledge.pipeline_factory import (
    KnowledgeRuntimeConfig,
    build_knowledge_pipeline,
    build_knowledge_retrieval_service,
    build_knowledge_service,
    runtime_config_from_settings,
)
from app.knowledge.query_planning import LLMQueryPlanner, SafeQueryPlanner
from app.knowledge.reranking import CompatibleReranker, DashScopeReranker, NoopReranker
from app.knowledge.service import KnowledgeService
from app.settings import Settings


def test_settings_map_to_an_immutable_runtime_config_with_compatible_defaults():
    config = runtime_config_from_settings(Settings())

    assert isinstance(config, KnowledgeRuntimeConfig)
    assert config.pipeline_enabled is False
    assert config.query_planning_enabled is False
    assert config.rerank_enabled is False
    assert config.candidate_limit == 30
    assert config.candidate_min_vector_similarity == 0.2
    assert config.allow_insufficient_llm is False

    with pytest.raises(ValidationError):
        config.pipeline_enabled = True


def test_runtime_config_validates_pipeline_limits_and_provider_settings():
    with pytest.raises(ValidationError):
        KnowledgeRuntimeConfig(candidate_limit=0)
    with pytest.raises(ValidationError):
        KnowledgeRuntimeConfig(candidate_min_vector_similarity=1.1)


def test_service_factory_keeps_pipeline_and_embedding_disabled_by_default():
    service = build_knowledge_service(
        object(),
        runtime_config_from_settings(
            Settings(embedding_api_key="", embedding_model="", embedding_dimensions=0)
        ),
    )

    assert isinstance(service, KnowledgeService)
    assert service.pipeline is None
    assert service.query_embedder is None
    assert service.embedding_model == ""
    assert service.embedding_dimensions == 0


def test_retrieval_factory_disables_pipeline_without_changing_embedding_settings():
    config = runtime_config_from_settings(
        Settings(
            embedding_api_key="embedding-key",
            embedding_base_url="https://embedding.example/v1",
            embedding_model="test-embedding",
            embedding_dimensions=3,
            knowledge_pipeline_enabled=True,
            knowledge_candidate_limit=17,
        )
    )

    online = build_knowledge_service(object(), config)
    retrieval = build_knowledge_retrieval_service(object(), config)

    assert online.pipeline is not None
    assert retrieval.pipeline is None
    assert retrieval.embedding_model == online.embedding_model == "test-embedding"
    assert retrieval.embedding_dimensions == online.embedding_dimensions == 3
    assert retrieval.candidate_limit == online.candidate_limit == 17
    assert retrieval.query_embedder is not None


def test_pipeline_factory_wires_the_same_production_component_types():
    config = runtime_config_from_settings(
        Settings(
            embedding_api_key="embedding-key",
            embedding_base_url="https://embedding.example/v1",
            embedding_model="test-embedding",
            embedding_dimensions=3,
            knowledge_pipeline_enabled=True,
            knowledge_query_planning_enabled=True,
            knowledge_rerank_enabled=True,
            knowledge_rerank_api_key="rerank-key",
            knowledge_rerank_base_url="https://rerank.example/v1",
            knowledge_rerank_model="test-reranker",
        )
    )

    pipeline = build_knowledge_pipeline(object(), config)
    service = build_knowledge_service(object(), config)

    assert pipeline is not None
    assert isinstance(pipeline.planner, LLMQueryPlanner)
    assert isinstance(pipeline.candidate_retriever, RepositoryCandidateRetriever)
    assert pipeline.candidate_retriever.query_embedder is not None
    assert isinstance(pipeline.reranker, CompatibleReranker)
    assert isinstance(pipeline.selector, CoverageAwareEvidenceSelector)
    assert isinstance(pipeline.judge, CoverageAnswerabilityJudge)

    assert service.pipeline is not None
    assert isinstance(service.pipeline.planner, LLMQueryPlanner)
    assert isinstance(service.query_embedder.__self__, EmbeddingClient)
    assert service.candidate_limit == 30


def test_pipeline_factory_selects_dashscope_reranker_explicitly():
    config = runtime_config_from_settings(
        Settings(
            embedding_api_key="embedding-key",
            embedding_base_url="https://embedding.example/v1",
            embedding_model="test-embedding",
            embedding_dimensions=3,
            knowledge_pipeline_enabled=True,
            knowledge_rerank_enabled=True,
            knowledge_rerank_provider="dashscope",
            knowledge_rerank_api_key="rerank-key",
            knowledge_rerank_base_url="https://maas.qianwenaiapi.com",
            knowledge_rerank_model="qwen3.7-text-rerank",
        )
    )

    pipeline = build_knowledge_pipeline(object(), config)

    assert pipeline is not None
    assert isinstance(pipeline.reranker, DashScopeReranker)


def test_pipeline_factory_rejects_unknown_reranker_provider():
    config = runtime_config_from_settings(
        Settings(
            knowledge_pipeline_enabled=True,
            knowledge_rerank_enabled=True,
            knowledge_rerank_provider="unknown",
            knowledge_rerank_api_key="rerank-key",
            knowledge_rerank_base_url="https://rerank.example/v1",
            knowledge_rerank_model="rerank-model",
        )
    )

    with pytest.raises(ValueError, match="unsupported knowledge rerank provider"):
        build_knowledge_pipeline(object(), config)


def test_pipeline_factory_uses_safe_planner_and_noop_reranker_when_disabled():
    config = runtime_config_from_settings(
        Settings(knowledge_pipeline_enabled=True)
    )

    pipeline = build_knowledge_pipeline(object(), config)

    assert pipeline is not None
    assert isinstance(pipeline.planner, SafeQueryPlanner)
    assert isinstance(pipeline.reranker, NoopReranker)


def test_service_factory_rejects_missing_reranker_configuration_before_service_creation():
    config = runtime_config_from_settings(
        Settings(
            knowledge_pipeline_enabled=True,
            knowledge_rerank_enabled=True,
        )
    )

    with pytest.raises(ValueError, match="rerank API key"):
        build_knowledge_service(object(), config)