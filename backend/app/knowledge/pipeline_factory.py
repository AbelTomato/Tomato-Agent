from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.knowledge.answerability import AnswerabilityConfig, CoverageAnswerabilityJudge
from app.knowledge.candidate_retrieval import RepositoryCandidateRetriever
from app.knowledge.embeddings import EmbeddingClient
from app.knowledge.evidence_selection import CoverageAwareEvidenceSelector
from app.knowledge.query_planning import LLMQueryPlanner, QueryPlannerConfig, SafeQueryPlanner
from app.knowledge.reranking import CompatibleReranker, DashScopeReranker, NoopReranker, Reranker
from app.knowledge.service import KnowledgePipeline, KnowledgeService
from app.settings import Settings


class KnowledgeRuntimeConfig(BaseModel):
    """Validated, immutable configuration shared by knowledge runtime entrypoints."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pipeline_enabled: bool = False
    query_planning_enabled: bool = False
    query_planning_structural_fallback_enabled: bool = False
    query_planning_max_queries: int = Field(default=4, ge=1, le=4)
    query_planning_max_query_chars: int = Field(default=300, ge=1, le=300)

    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_model: str = ""
    embedding_dimensions: int = 0
    embedding_timeout_seconds: float = Field(default=60.0, gt=0)

    min_vector_similarity: float = Field(default=0.5, ge=0.0, le=1.0)
    candidate_limit: int = Field(default=30, gt=0)
    candidate_min_vector_similarity: float = Field(default=0.2, ge=0.0, le=1.0)
    candidate_keyword_limit: int = Field(default=20, gt=0)
    candidate_vector_limit: int = Field(default=30, gt=0)

    answerability_min_supported_coverage: float = Field(default=1.0, ge=0.0, le=1.0)
    answerability_min_partial_coverage: float = Field(default=0.5, ge=0.0, le=1.0)
    answerability_min_supported_evidence: int = Field(default=1, gt=0)
    answerability_multi_evidence_requires_all_queries: bool = True
    allow_insufficient_llm: bool = False

    rerank_enabled: bool = False
    rerank_provider: str = "compatible"
    rerank_api_key: str = ""
    rerank_base_url: str = ""
    rerank_model: str = ""
    rerank_timeout_seconds: float = Field(default=10.0, gt=0)
    rerank_candidate_limit: int = Field(default=30, gt=0)

    def answerability_config(self) -> AnswerabilityConfig:
        return AnswerabilityConfig(
            min_supported_coverage=self.answerability_min_supported_coverage,
            min_partial_coverage=self.answerability_min_partial_coverage,
            min_supported_evidence=self.answerability_min_supported_evidence,
            multi_evidence_requires_all_queries=self.answerability_multi_evidence_requires_all_queries,
            allow_insufficient_llm=self.allow_insufficient_llm,
        )


def runtime_config_from_settings(settings: Settings) -> KnowledgeRuntimeConfig:
    """Translate application settings into the shared immutable knowledge config."""

    return KnowledgeRuntimeConfig(
        pipeline_enabled=settings.knowledge_pipeline_enabled,
        query_planning_enabled=settings.knowledge_query_planning_enabled,
        query_planning_structural_fallback_enabled=(
            settings.knowledge_query_planning_structural_fallback_enabled
        ),
        query_planning_max_queries=settings.knowledge_query_planning_max_queries,
        query_planning_max_query_chars=settings.knowledge_query_planning_max_query_chars,
        embedding_api_key=settings.embedding_api_key,
        embedding_base_url=settings.embedding_base_url,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        embedding_timeout_seconds=settings.embedding_timeout_seconds,
        min_vector_similarity=settings.knowledge_min_vector_similarity,
        candidate_limit=settings.knowledge_candidate_limit,
        candidate_min_vector_similarity=settings.knowledge_candidate_min_vector_similarity,
        candidate_keyword_limit=settings.knowledge_candidate_keyword_limit,
        candidate_vector_limit=settings.knowledge_candidate_vector_limit,
        answerability_min_supported_coverage=settings.knowledge_answerability_min_supported_coverage,
        answerability_min_partial_coverage=settings.knowledge_answerability_min_partial_coverage,
        answerability_min_supported_evidence=settings.knowledge_answerability_min_supported_evidence,
        answerability_multi_evidence_requires_all_queries=(
            settings.knowledge_answerability_multi_evidence_requires_all_queries
        ),
        allow_insufficient_llm=settings.knowledge_answerability_allow_insufficient_llm,
        rerank_enabled=settings.knowledge_rerank_enabled,
        rerank_provider=settings.knowledge_rerank_provider,
        rerank_api_key=settings.knowledge_rerank_api_key,
        rerank_base_url=settings.knowledge_rerank_base_url,
        rerank_model=settings.knowledge_rerank_model,
        rerank_timeout_seconds=settings.knowledge_rerank_timeout_seconds,
        rerank_candidate_limit=settings.knowledge_rerank_candidate_limit,
    )


def runtime_config_for_evaluation_profile(
    args: Any,
    *,
    settings: Settings,
    embedding_client: Any | None = None,
    reranker: Any | None = None,
) -> KnowledgeRuntimeConfig:
    """Build the fixed production-equivalent runtime config for evaluation.

    The evaluation profile is deliberately stricter than the online defaults:
    it must have every provider needed by the complete chain before a request
    can be made.  In particular, it never turns a missing Embedding or
    reranker provider into keyword/noop fallback.
    """

    if getattr(args, "profile", None) != "production-equivalent":
        raise ValueError("production-equivalent profile is required")
    if args.split != "dev":
        raise ValueError("production-equivalent profile accepts only split=dev")
    if args.mode != "hybrid":
        raise ValueError("production-equivalent profile requires hybrid mode")
    if args.candidate_limit != 30 or args.candidate_min_vector_similarity != 0.2:
        raise ValueError("production-equivalent profile requires candidate_limit=30 and threshold=0.2")
    if args.final_limit != 5:
        raise ValueError("production-equivalent profile requires final_limit=5")
    if args.query_planning != "conditional":
        raise ValueError("production-equivalent profile requires conditional query planning")
    if args.rerank != "provider":
        raise ValueError("production-equivalent profile requires provider reranking")
    if args.evidence_selection != "coverage-aware":
        raise ValueError("production-equivalent profile requires coverage-aware selection")
    if args.answerability != "coverage-v1":
        raise ValueError("production-equivalent profile requires coverage-v1 answerability")
    if getattr(args, "allow_insufficient_llm", False):
        raise ValueError("production-equivalent profile requires allow_insufficient_llm=false")

    if embedding_client is None and not settings.embedding_api_key.strip():
        raise ValueError("production-equivalent profile requires an embedding API key")
    if embedding_client is None and not args.embedding_model.strip():
        raise ValueError("production-equivalent profile requires embedding model and dimensions")
    if args.embedding_dimensions <= 0:
        raise ValueError("production-equivalent profile requires embedding model and dimensions")
    if reranker is None and not settings.knowledge_rerank_api_key.strip():
        raise ValueError("production-equivalent profile requires a rerank API key")
    if reranker is None and (
        not settings.knowledge_rerank_base_url.strip() or not settings.knowledge_rerank_model.strip()
    ):
        raise ValueError("production-equivalent profile requires rerank base URL and model")

    return runtime_config_from_settings(settings).model_copy(
        update={
            "pipeline_enabled": True,
            "query_planning_enabled": True,
            "query_planning_structural_fallback_enabled": True,
            "embedding_model": args.embedding_model,
            "embedding_dimensions": args.embedding_dimensions,
            "candidate_limit": 30,
            "candidate_min_vector_similarity": 0.2,
            "answerability_min_supported_coverage": 1.0,
            "answerability_min_partial_coverage": 0.5,
            "answerability_min_supported_evidence": 1,
            "answerability_multi_evidence_requires_all_queries": True,
            "allow_insufficient_llm": False,
            "rerank_enabled": True,
            "rerank_candidate_limit": 30,
        }
    )


def runtime_config_manifest(config: KnowledgeRuntimeConfig) -> dict[str, Any]:
    """Return a credential-free effective config suitable for a Trace manifest."""

    return {
        "pipeline_enabled": config.pipeline_enabled,
        "query_planning_enabled": config.query_planning_enabled,
        "query_planning_structural_fallback_enabled": config.query_planning_structural_fallback_enabled,
        "query_planning_max_queries": config.query_planning_max_queries,
        "query_planning_max_query_chars": config.query_planning_max_query_chars,
        "embedding_provider_configured": bool(
            config.embedding_api_key.strip()
            and config.embedding_model.strip()
            and config.embedding_dimensions > 0
        ),
        "embedding_base_url": config.embedding_base_url,
        "embedding_model": config.embedding_model,
        "embedding_dimensions": config.embedding_dimensions,
        "candidate_limit": config.candidate_limit,
        "candidate_min_vector_similarity": config.candidate_min_vector_similarity,
        "rerank_enabled": config.rerank_enabled,
        "rerank_provider": config.rerank_provider,
        "rerank_provider_configured": bool(
            config.rerank_api_key.strip()
            and config.rerank_base_url.strip()
            and config.rerank_model.strip()
        ),
        "rerank_base_url": config.rerank_base_url,
        "rerank_model": config.rerank_model,
        "rerank_candidate_limit": config.rerank_candidate_limit,
        "evidence_selection": "coverage-aware",
        "answerability": "coverage-v1",
        "allow_insufficient_llm": config.allow_insufficient_llm,
    }


def create_embedding_client(config: KnowledgeRuntimeConfig) -> EmbeddingClient | None:
    if (
        not config.embedding_api_key.strip()
        or not config.embedding_model.strip()
        or config.embedding_dimensions <= 0
    ):
        return None
    return EmbeddingClient(
        api_key=config.embedding_api_key,
        base_url=config.embedding_base_url,
        model=config.embedding_model,
        dimensions=config.embedding_dimensions,
        timeout_seconds=config.embedding_timeout_seconds,
    )


def create_reranker(config: KnowledgeRuntimeConfig):
    if not config.rerank_enabled:
        return NoopReranker()
    if not config.rerank_api_key.strip():
        raise ValueError("knowledge rerank API key is required when rerank is enabled")
    reranker_type = {
        "compatible": CompatibleReranker,
        "dashscope": DashScopeReranker,
    }.get(config.rerank_provider.strip().lower())
    if reranker_type is None:
        raise ValueError(f"unsupported knowledge rerank provider: {config.rerank_provider}")
    return reranker_type(
        api_key=config.rerank_api_key,
        base_url=config.rerank_base_url,
        model=config.rerank_model,
        timeout_seconds=config.rerank_timeout_seconds,
    )


def build_knowledge_pipeline(
    repository: Any,
    config: KnowledgeRuntimeConfig,
    *,
    embedding_client: EmbeddingClient | None = None,
    reranker: Reranker | None = None,
    observer=None,
) -> KnowledgePipeline | None:
    """Build the configured production knowledge pipeline, if enabled."""

    if not config.pipeline_enabled:
        return None

    embedding_client = embedding_client or create_embedding_client(config)
    planner_config = QueryPlannerConfig(
        enabled=config.query_planning_enabled,
        structural_fallback_enabled=config.query_planning_structural_fallback_enabled,
        max_queries=config.query_planning_max_queries,
        max_query_chars=config.query_planning_max_query_chars,
    )
    planner = (
        LLMQueryPlanner(planner_config)
        if config.query_planning_enabled
        else SafeQueryPlanner(planner_config)
    )
    candidate_retriever = RepositoryCandidateRetriever(
        repository,
        query_embedder=embedding_client.embed if embedding_client is not None else None,
        embedding_model=config.embedding_model,
        embedding_dimensions=config.embedding_dimensions,
        candidate_min_vector_similarity=config.candidate_min_vector_similarity,
    )
    return KnowledgePipeline(
        planner=planner,
        candidate_retriever=candidate_retriever,
        reranker=reranker or create_reranker(config),
        selector=CoverageAwareEvidenceSelector(),
        judge=CoverageAnswerabilityJudge(),
        answerability_config=config.answerability_config(),
        observer=observer,
    )


def build_knowledge_service(
    repository: Any,
    config: KnowledgeRuntimeConfig,
    *,
    embedding_client: EmbeddingClient | None = None,
    reranker: Reranker | None = None,
    observer=None,
) -> KnowledgeService:
    """Build the knowledge service and its optional shared retrieval pipeline."""

    embedding_client = embedding_client or create_embedding_client(config)
    pipeline = build_knowledge_pipeline(
        repository,
        config,
        embedding_client=embedding_client,
        reranker=reranker,
        observer=observer,
    )
    return KnowledgeService(
        repository,
        query_embedder=embedding_client.embed if embedding_client is not None else None,
        embedding_model=config.embedding_model,
        embedding_dimensions=config.embedding_dimensions,
        min_vector_similarity=config.min_vector_similarity,
        pipeline=pipeline,
        candidate_limit=config.candidate_limit,
        allow_insufficient_llm=config.allow_insufficient_llm,
    )


def build_knowledge_retrieval_service(
    repository: Any,
    config: KnowledgeRuntimeConfig,
    *,
    embedding_client: EmbeddingClient | None = None,
) -> KnowledgeService:
    """Build a deterministic retrieval-only service without the online pipeline."""

    retrieval_config = config.model_copy(update={"pipeline_enabled": False})
    return build_knowledge_service(
        repository,
        retrieval_config,
        embedding_client=embedding_client,
    )