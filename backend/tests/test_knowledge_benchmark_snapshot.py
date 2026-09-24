import json
import sqlite3
from pathlib import Path

import pytest

from app.knowledge.benchmark_snapshot import (
    fingerprint_knowledge_database,
    load_questions,
    open_read_only_database,
)


def make_database(path: Path, *, document_text: str = "text", session_value: str = "one") -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE documents (
                document_id TEXT PRIMARY KEY, source_path TEXT NOT NULL,
                source_url TEXT, title TEXT NOT NULL, content_hash TEXT NOT NULL,
                index_status TEXT NOT NULL, index_error TEXT
            );
            CREATE TABLE chunks (
                chunk_id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                document_version TEXT NOT NULL, heading_path TEXT NOT NULL,
                start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
                text TEXT NOT NULL, token_count INTEGER NOT NULL
            );
            CREATE TABLE embeddings (
                chunk_id TEXT NOT NULL, document_id TEXT NOT NULL,
                document_version TEXT NOT NULL, model TEXT NOT NULL,
                dimensions INTEGER NOT NULL, text_hash TEXT NOT NULL,
                vector_json TEXT NOT NULL
            );
            CREATE TABLE sessions (id TEXT PRIMARY KEY, value TEXT);
            """
        )
        db.execute("INSERT INTO documents VALUES (?, ?, NULL, ?, ?, 'ready', NULL)",
                   ("doc-1", "doc.md", "Document", "version-1"))
        db.execute("INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   ("chunk-1", "doc-1", "version-1", "", 1, 1, document_text, 1))
        db.execute("INSERT INTO embeddings VALUES (?, ?, ?, ?, ?, ?, ?)",
                   ("chunk-1", "doc-1", "version-1", "model", 2, "hash", "[0.1,0.2]"))
        db.execute("INSERT INTO sessions VALUES ('session-1', ?)", (session_value,))


def test_knowledge_fingerprint_is_order_independent_and_tracks_content(tmp_path: Path):
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    make_database(first)
    make_database(second)

    assert fingerprint_knowledge_database(first) == fingerprint_knowledge_database(second)
    make_database_content = sqlite3.connect(second)
    make_database_content.execute("UPDATE chunks SET text = 'changed'")
    make_database_content.commit()
    make_database_content.close()
    assert fingerprint_knowledge_database(first) != fingerprint_knowledge_database(second)


def test_session_changes_do_not_affect_knowledge_fingerprint(tmp_path: Path):
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    make_database(first)
    make_database(second, session_value="different")

    assert fingerprint_knowledge_database(first) == fingerprint_knowledge_database(second)


def test_read_only_open_does_not_create_or_mutate_database(tmp_path: Path):
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        open_read_only_database(missing)
    assert not missing.exists()

    database = tmp_path / "knowledge.db"
    make_database(database)
    before = database.read_bytes()
    connection = open_read_only_database(database)
    with pytest.raises(sqlite3.OperationalError):
        connection.execute("CREATE TABLE unexpected (id INTEGER)")
    connection.close()
    assert database.read_bytes() == before


def test_read_only_open_rejects_incompatible_schema(tmp_path: Path):
    database = tmp_path / "incompatible.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE documents (id TEXT)")
    with pytest.raises(ValueError, match="incompatible"):
        open_read_only_database(database)


def test_load_questions_validates_selected_split_and_line_bounds(tmp_path: Path):
    dataset = tmp_path / "questions.jsonl"
    dataset.write_text(
        json.dumps({
            "id": "q1", "group_id": "g1", "query": "question", "category": "term",
            "split": "dev", "answerable": True,
            "relevant_spans": [{"document_id": "doc-1", "document_version": "v1",
                                "start_line": 1, "end_line": 2}],
            "reference_answer": "answer",
        }) + "\n" + json.dumps({
            "id": "q2", "group_id": "g2", "query": "held out", "category": "term",
            "split": "test", "answerable": False, "relevant_spans": [],
            "reference_answer": "refuse",
        }) + "\n",
        encoding="utf-8",
    )
    questions = load_questions(dataset, split="dev", document_line_counts={"doc-1": 2})
    assert [question["id"] for question in questions] == ["q1"]

    with pytest.raises(ValueError, match="outside"):
        load_questions(dataset, split="dev", document_line_counts={"doc-1": 1})


def test_load_questions_does_not_parse_other_split_content(tmp_path: Path):
    dataset = tmp_path / "questions.jsonl"
    dev_question = {
        "id": "q1", "group_id": "g1", "query": "question", "category": "term",
        "split": "dev", "answerable": True,
        "relevant_spans": [{"document_id": "doc-1", "document_version": "v1",
                            "start_line": 1, "end_line": 1}],
        "reference_answer": "answer",
    }
    dataset.write_text(
        json.dumps(dev_question) + '\n{"split":"test","sentinel":"must-not-parse"\n',
        encoding="utf-8",
    )

    questions = load_questions(dataset, split="dev", document_line_counts={"doc-1": 1})

    assert [question["id"] for question in questions] == ["q1"]


def test_load_questions_rejects_unanswerable_evidence(tmp_path: Path):
    dataset = tmp_path / "questions.jsonl"
    records = [
        {"id": "q1", "group_id": "g1", "query": "one", "category": "term",
         "split": "dev", "answerable": False,
         "relevant_spans": [{"document_id": "doc-1", "document_version": "v1",
                             "start_line": 1, "end_line": 1}], "reference_answer": "b"},
    ]
    dataset.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    with pytest.raises(ValueError, match="unanswerable"):
        load_questions(dataset, split="dev", document_line_counts={"doc-1": 1})


def test_read_jsonl_rejects_group_leakage(tmp_path: Path):
    from app.knowledge.benchmark_snapshot import _read_jsonl

    dataset = tmp_path / "questions.jsonl"
    records = [
        {"id": "q1", "group_id": "shared", "query": "one", "category": "term",
         "split": "dev", "answerable": True, "relevant_spans": []},
        {"id": "q2", "group_id": "shared", "query": "two", "category": "term",
         "split": "test", "answerable": False, "relevant_spans": []},
    ]
    dataset.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    with pytest.raises(ValueError, match="group_id"):
        _read_jsonl(dataset)