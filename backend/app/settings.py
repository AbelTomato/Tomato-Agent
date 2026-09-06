from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_path: Path = Path("data/agent.db")
    docs_root: Path = Path("../docs")
    max_doc_bytes: int = 100_000
    max_tool_result_chars: int = 20_000
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
