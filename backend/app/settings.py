from pathlib import Path

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
