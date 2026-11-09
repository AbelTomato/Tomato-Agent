"""Isolate pytest from local Knowledge pipeline settings in backend/.env."""

import os
import pytest

# Set before importing app.settings, including its module-level singleton.
os.environ["CONFIG_FILE"] = ""


# pytest loads conftest.py before importing test modules. Environment variables
# take precedence over Pydantic's configured .env file, so tests remain stable
# regardless of the caller's working directory or local provider settings.
os.environ["KNOWLEDGE_PIPELINE_ENABLED"] = "false"
os.environ["KNOWLEDGE_QUERY_PLANNING_ENABLED"] = "false"
os.environ["KNOWLEDGE_RERANK_ENABLED"] = "false"
os.environ["KNOWLEDGE_RERANK_PROVIDER"] = "compatible"
os.environ["KNOWLEDGE_RERANK_API_KEY"] = ""


@pytest.fixture
def create_symlink():
    def create(link, target, *, target_is_directory=False):
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows symbolic-link privilege is unavailable")
            raise
    return create
