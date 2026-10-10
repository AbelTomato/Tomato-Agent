from pathlib import Path

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agent.config import RuntimeConfig
from app.agent.models import CodeTaskBudget
from app.agent.policies import CodeTaskCapabilityProfile


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
    llm_model: str = "gpt-5.6-luna"
    llm_timeout_seconds: float = 60.0
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_model: str = ""
    embedding_dimensions: int = 0
    embedding_timeout_seconds: float = 60.0
    knowledge_min_vector_similarity: float = 0.5
    knowledge_query_planning_enabled: bool = False
    knowledge_query_planning_structural_fallback_enabled: bool = False
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
    knowledge_rerank_provider: str = "compatible"
    knowledge_rerank_api_key: str = ""
    knowledge_rerank_base_url: str = ""
    knowledge_rerank_model: str = ""
    knowledge_rerank_timeout_seconds: float = Field(default=10.0, gt=0)
    knowledge_rerank_candidate_limit: int = Field(default=30, gt=0)
    draft_directory: Path = Path("data/drafts")
    writing_retrieval_mode: Literal["keyword", "vector", "hybrid"] = "hybrid"
    writing_max_evidence: int = Field(default=5, ge=1, le=10)
    writing_max_context_tokens: int = Field(default=8000, gt=0)
    writing_max_response_chars: int = Field(default=20000, gt=0)
    writing_timeout_seconds: float = Field(default=120.0, gt=0)
    code_task_allow_network: Literal[False] = False
    code_task_allow_credentials: Literal[False] = False
    code_task_worker_enabled: bool = False
    code_task_worker_poll_interval_seconds: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    code_task_worker_max_concurrency: Literal[1] = 1
    code_task_max_loops: int = Field(default=12, gt=0)
    code_task_max_tool_calls: int = Field(default=8, gt=0)
    code_task_max_duration_seconds: float = Field(default=120.0, gt=0, allow_inf_nan=False)
    code_task_max_context_tokens: int = Field(default=8000, gt=0)
    code_task_max_response_chars: int = Field(default=20000, gt=0)
    code_task_tool_timeout_seconds: float = Field(default=20.0, gt=0, allow_inf_nan=False)
    code_task_max_tool_result_chars: int = Field(default=20000, gt=0)
    code_task_max_file_bytes: int = Field(default=100000, gt=0)
    code_task_max_artifact_bytes: int = Field(default=1000000, gt=0)
    code_task_workspace_root: Path = Path("data/code-workspaces")
    code_task_artifact_root: Path = Path("data/code-artifacts")
    code_task_sandbox_backend: Literal["local", "docker", "openshell"] = "local"
    code_task_docker_image: str = ""
    code_task_docker_binary: str = "docker"
    code_task_openshell_image: str = ""
    code_task_openshell_binary: str = "openshell"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def code_task_budget(self) -> CodeTaskBudget:
        return CodeTaskBudget(
            max_loops=self.code_task_max_loops,
            max_tool_calls=self.code_task_max_tool_calls,
            max_duration_seconds=self.code_task_max_duration_seconds,
            max_context_tokens=self.code_task_max_context_tokens,
            max_response_chars=self.code_task_max_response_chars,
        )

    def code_task_capability_profile(self, workspace_root: Path) -> CodeTaskCapabilityProfile:
        return CodeTaskCapabilityProfile(
            sandbox_backend=self.code_task_sandbox_backend,
            allowed_paths=(str(workspace_root.resolve()),),
            timeout_seconds=self.code_task_tool_timeout_seconds,
            max_output_chars=self.code_task_max_tool_result_chars,
        )

    @property
    def runtime_config(self) -> RuntimeConfig:
        return RuntimeConfig(
            max_loops=self.runtime_max_loops,
            max_tool_calls=self.runtime_max_tool_calls,
            max_duration_seconds=self.runtime_max_duration_seconds,
            tool_timeout_seconds=self.runtime_tool_timeout_seconds,
        )


settings = Settings()
