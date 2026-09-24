"""Deterministic, read-only snapshot and dataset helpers for retrieval benchmarks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from app.knowledge.benchmark_models import EvidenceSpan


_REQUIRED_TABLE_COLUMNS = {
    "documents": {"document_id", "source_path", "source_url", "title", "content_hash", "index_status"},
    "chunks": {
        "chunk_id", "document_id", "document_version", "heading_path",
        "start_line", "end_line", "text", "token_count",
    },
    "embeddings": {
        "chunk_id", "document_id", "document_version", "model", "dimensions",
        "text_hash", "vector_json",
    },
}


def open_read_only_database(path: Path) -> sqlite3.Connection:
    """Open an existing compatible knowledge DB without creating or migrating it."""
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"knowledge database does not exist: {path}")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table, required_columns in _REQUIRED_TABLE_COLUMNS.items():
            if table not in tables:
                raise ValueError(f"incompatible knowledge database: missing table {table}")
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            missing = required_columns - columns
            if missing:
                raise ValueError(
                    f"incompatible knowledge database: {table} missing columns {sorted(missing)}"
                )
        return connection
    except Exception:
        connection.close()
        raise


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def fingerprint_knowledge_database(path: Path) -> str:
    """Hash normalized knowledge rows; session/business tables are intentionally ignored."""
    connection = open_read_only_database(path)
    try:
        data: dict[str, list[list[Any]]] = {}
        columns = {
            "documents": (
                "document_id", "source_path", "source_url", "title", "content_hash", "index_status", "index_error"
            ),
            "chunks": (
                "chunk_id", "document_id", "document_version", "heading_path",
                "start_line", "end_line", "text", "token_count",
            ),
            "embeddings": (
                "chunk_id", "document_id", "document_version", "model", "dimensions",
                "text_hash", "vector_json",
            ),
        }
        for table, fields in columns.items():
            available = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            selected = [field for field in fields if field in available]
            query = f"SELECT {', '.join(selected)} FROM {table} ORDER BY {', '.join(selected)}"
            data[table] = [list(row) for row in connection.execute(query)]
        return _canonical_hash(data)
    finally:
        connection.close()


def _read_jsonl(path: Path, *, split: str | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    groups_to_splits: dict[str, set[str]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        selected_split = split
        if selected_split is not None:
            try:
                split_value = json.loads(line).get("split")
            except (json.JSONDecodeError, AttributeError):
                split_value = None
            if split_value != selected_split:
                continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid question JSON on line {line_number}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"question on line {line_number} must be an object")
        required = {"id", "group_id", "query", "category", "split", "answerable", "relevant_spans"}
        if not required <= item.keys():
            raise ValueError(f"question on line {line_number} is missing fields: {sorted(required - item.keys())}")
        question_id, group_id, record_split = item["id"], item["group_id"], item["split"]
        if not all(isinstance(value, str) and value.strip() for value in (question_id, group_id, record_split)):
            raise ValueError(f"question on line {line_number} has invalid id, group_id, or split")
        if question_id in seen_ids:
            raise ValueError(f"Duplicate question_id: {question_id}")
        seen_ids.add(question_id)
        groups_to_splits.setdefault(group_id, set()).add(record_split)
        records.append(item)
    leaked = sorted(group for group, splits in groups_to_splits.items() if len(splits) > 1)
    if leaked:
        raise ValueError(f"group_id leakage across splits: {', '.join(leaked)}")
    return records


def load_questions(
    path: Path, *, split: str, document_line_counts: dict[str, int]
) -> tuple[dict[str, Any], ...]:
    """Load and validate one split while retaining all IDs for leakage checks."""
    if not split.strip():
        raise ValueError("split must not be blank")
    selected = _read_jsonl(path, split=split)
    if not selected:
        raise ValueError(f"dataset has no questions for split: {split}")
    groups: dict[str, set[str]] = {}
    for item in selected:
        groups.setdefault(item["group_id"], set()).add(item["split"])
    leaked = sorted(group for group, splits in groups.items() if len(splits) > 1)
    if leaked:
        raise ValueError(f"group_id leakage across splits: {', '.join(leaked)}")
    normalized: list[dict[str, Any]] = []
    for item in selected:
        if not isinstance(item["answerable"], bool):
            raise ValueError(f"question {item['id']} answerable must be boolean")
        spans = item["relevant_spans"]
        if not isinstance(spans, list):
            raise ValueError(f"question {item['id']} relevant_spans must be an array")
        if not item["answerable"] and spans:
            raise ValueError(f"unanswerable question {item['id']} must have empty evidence")
        validated_spans: list[dict[str, Any]] = []
        for span in spans:
            evidence = EvidenceSpan.model_validate(span)
            max_line = document_line_counts.get(evidence.document_id)
            if max_line is None or evidence.end_line > max_line:
                raise ValueError(f"question {item['id']} evidence line range is outside its document")
            validated_spans.append(evidence.model_dump())
        if item["answerable"] and not validated_spans:
            raise ValueError(f"answerable question {item['id']} must have evidence")
        normalized.append({**item, "relevant_spans": validated_spans})
    return tuple(normalized)