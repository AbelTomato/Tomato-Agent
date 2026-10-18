"""Non-secret TOML configuration, mapped to the existing Settings interface."""

from pathlib import Path
import tomllib


BACKEND_ROOT = Path(__file__).resolve().parents[1]

GROUPS = {
    "assistant": {"enabled": "assistant_enabled"},
    "worker": {"enabled": "task_worker_enabled", "poll_interval_seconds": "code_task_worker_poll_interval_seconds"},
    "storage": {name: name for name in (
        "database_path", "docs_root", "draft_directory", "code_task_workspace_root", "code_task_artifact_root")},
    "runtime": {**{name: "runtime_" + name for name in (
        "max_loops", "max_tool_calls", "max_duration_seconds", "tool_timeout_seconds")},
        "max_doc_bytes": "max_doc_bytes", "max_tool_result_chars": "max_tool_result_chars"},
    "providers": {name: name for name in ("llm_timeout_seconds", "embedding_timeout_seconds", "llm_diagnostics_enabled", "llm_diagnostics_root")},
    "knowledge": {name: "knowledge_" + name for name in (
        "pipeline_enabled", "min_vector_similarity", "candidate_limit", "candidate_min_vector_similarity",
        "candidate_keyword_limit", "candidate_vector_limit")},
    "knowledge.query_planning": {name: "knowledge_query_planning_" + name for name in (
        "enabled", "structural_fallback_enabled", "max_queries", "max_query_chars")},
    "knowledge.answerability": {name: "knowledge_answerability_" + name for name in (
        "min_supported_coverage", "min_partial_coverage", "min_supported_evidence",
        "multi_evidence_requires_all_queries", "allow_insufficient_llm")},
    "knowledge.rerank": {name: "knowledge_rerank_" + name for name in (
        "enabled", "provider", "timeout_seconds", "candidate_limit")},
    "writing": {name: "writing_" + name for name in (
        "retrieval_mode", "max_evidence", "max_context_tokens", "max_response_chars", "timeout_seconds")},
    "code": {**{name: "code_task_" + name for name in (
        "sandbox_backend", "docker_image", "docker_binary", "openshell_image", "openshell_binary",
        "max_loops", "max_tool_calls", "max_duration_seconds", "max_context_tokens", "max_response_chars",
        "tool_timeout_seconds", "max_tool_result_chars", "max_file_bytes", "max_artifact_bytes")},
        "workspace_allowed_roots": "workspace_allowed_roots"},
}


def read_config(path: Path, *, required: bool) -> dict:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        if not required:
            return {}
        raise ValueError("Explicit configuration file does not exist") from None
    except (OSError, tomllib.TOMLDecodeError):
        raise ValueError("Cannot read configuration file or invalid TOML syntax") from None

    result = {}

    def visit(table, section=""):
        for key, value in table.items():
            dotted = f"{section}.{key}" if section else key
            if isinstance(value, dict):
                if dotted not in GROUPS:
                    raise ValueError("Unknown TOML configuration section")
                visit(value, dotted)
            else:
                field = GROUPS.get(section, {}).get(key)
                if field is None:
                    raise ValueError("Unknown or prohibited TOML configuration option")
                result[field] = value

    visit(data)
    return result


def normalize_worker(data: dict) -> dict:
    data = dict(data)
    # Normalize aliases within each source, before Pydantic merges sources.
    if data.get("task_worker_enabled") is None and "code_task_worker_enabled" in data:
        data["task_worker_enabled"] = data["code_task_worker_enabled"]
    return data