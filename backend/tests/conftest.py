"""Isolate pytest from local Knowledge pipeline settings in backend/.env."""

import os


# pytest loads conftest.py before importing test modules. Environment variables
# take precedence over Pydantic's configured .env file, so tests remain stable
# regardless of the caller's working directory or local provider settings.
os.environ["KNOWLEDGE_PIPELINE_ENABLED"] = "false"
os.environ["KNOWLEDGE_QUERY_PLANNING_ENABLED"] = "false"
os.environ["KNOWLEDGE_RERANK_ENABLED"] = "false"
os.environ["KNOWLEDGE_RERANK_PROVIDER"] = "compatible"
os.environ["KNOWLEDGE_RERANK_API_KEY"] = ""