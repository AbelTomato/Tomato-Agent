from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agent.config import RuntimeConfig


class Settings(BaseSettings):
    database_path: Path = Path("data/agent.db")
    docs_root: Path = Path("../docs")
    max_doc_bytes: int = 100_000
    max_tool_result_chars: int = 20_000
    runtime_max_loops: int = 12
    runtime_max_tool_calls: int = 8
    runtime_max_duration_seconds: float = 120.0
    runtime_tool_timeout_seconds: float = 20.0
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-5.6-terra"
    llm_timeout_seconds: float = 60.0
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_model: str = ""
    embedding_dimensions: int = 0
    embedding_timeout_seconds: float = 60.0
    knowledge_min_vector_similarity: float = 0.5
    knowledge_query_planning_enabled: bool = False
    knowledge_query_planning_max_queries: int = 4
    knowledge_query_planning_max_query_chars: int = 300
    knowledge_candidate_limit: int = 30
    knowledge_candidate_min_vector_similarity: float = 0.2
    knowledge_candidate_keyword_limit: int = 20
    knowledge_candidate_vector_limit: int = 30
    knowledge_pipeline_enabled: bool = False
    knowledge_answerability_min_supported_coverage: float = Field(default=1.0, ge=0, le=1)
    knowledge_answerability_min_partial_coverage: float = Field(default=0.5, ge=0, le=1)
    knowledge_answerability_min_supported_evidence: int = Field(default=1, gt=0)
    knowledge_answerability_multi_evidence_requires_all_queries: bool = True
    knowledge_answerability_allow_insufficient_llm: bool = False
    knowledge_rerank_enabled: bool = False
    knowledge_rerank_api_key: str = ""
    knowledge_rerank_base_url: str = ""
    knowledge_rerank_model: str = ""
    knowledge_rerank_timeout_seconds: float = Field(default=10.0, gt=0)
    knowledge_rerank_candidate_limit: int = Field(default=30, gt=0)
    draft_directory: Path = Path("data/drafts")
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def runtime_config(self) -> RuntimeConfig:
        return RuntimeConfig(
            max_loops=self.runtime_max_loops,
            max_tool_calls=self.runtime_max_tool_calls,
            max_duration_seconds=self.runtime_max_duration_seconds,
            tool_timeout_seconds=self.runtime_tool_timeout_seconds,
        )


settings = Settings()
