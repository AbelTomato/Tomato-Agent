"""Load and validate the approved, reproducible Harness stage 2 task set."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .harness_stage2 import FixedTask


class TaskSetEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    citation_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    document_version: str = Field(min_length=1)
    source_path: str = Field(min_length=1)
    source_url: str | None = None
    title: str = Field(min_length=1)
    heading_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    text: str = Field(min_length=1)


class LoadedTaskSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    dataset_path: str
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    knowledge_snapshot_path: str
    knowledge_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    split: str
    tasks: tuple[FixedTask, ...]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_questions(path: Path) -> dict[str, dict[str, Any]]:
    questions: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        question_id = row["id"]
        if question_id in questions:
            raise ValueError(f"duplicate source question ID: {question_id}")
        questions[question_id] = row
    return questions


def _evidence_for_span(connection: sqlite3.Connection, span: dict[str, Any]) -> TaskSetEvidence:
    rows = connection.execute(
        """
        SELECT c.chunk_id, c.document_id, c.document_version,
               d.source_path, d.source_url, d.title, c.heading_path,
               c.start_line, c.end_line, c.text
        FROM chunks AS c
        JOIN documents AS d ON d.document_id = c.document_id
        WHERE c.document_id = ? AND c.document_version = ?
          AND c.start_line <= ? AND c.end_line >= ?
        ORDER BY (c.end_line - c.start_line), c.start_line, c.chunk_id
        """,
        (span["document_id"], span["document_version"], span["start_line"], span["end_line"]),
    ).fetchall()
    if not rows:
        raise ValueError(f"evidence span has no matching chunk: {span}")
    row = rows[0]
    return TaskSetEvidence(
        citation_id=row[0], chunk_id=row[0], document_id=row[1],
        document_version=row[2], source_path=row[3], source_url=row[4],
        title=row[5], heading_path=row[6], start_line=row[7], end_line=row[8], text=row[9],
    )


def load_stage2_task_set(manifest_path: Path | None = None) -> LoadedTaskSet:
    manifest_path = manifest_path or Path(__file__).with_name("harness_stage2_taskset.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = Path(__file__).resolve().parents[2]
    dataset_path = (base / manifest["dataset"]).resolve()
    snapshot_path = (base / manifest["knowledge_snapshot"]).resolve()
    if _sha256_file(dataset_path) != manifest["dataset_sha256"]:
        raise ValueError("Harness source dataset hash does not match the approved manifest")
    if _sha256_file(snapshot_path) != manifest["knowledge_snapshot_sha256"]:
        raise ValueError("Harness knowledge snapshot hash does not match the approved manifest")

    questions = _read_questions(dataset_path)
    tasks: list[FixedTask] = []
    with sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only = ON")
        for spec in manifest["tasks"]:
            question = questions.get(spec["source_question_id"])
            if question is None:
                raise ValueError(f"unknown source question: {spec['source_question_id']}")
            if question.get("split") != manifest["split"]:
                raise ValueError(f"source question is outside approved split: {spec['source_question_id']}")
            evidence = tuple(
                _evidence_for_span(connection, span)
                for span in question.get("relevant_spans", [])
            )
            if question.get("answerable") and not evidence:
                raise ValueError(f"answerable task has no evidence: {spec['source_question_id']}")
            if not question.get("answerable") and evidence:
                raise ValueError(f"unanswerable task unexpectedly has evidence: {spec['source_question_id']}")
            snapshot = {
                "schema_version": "citation-snapshot/v1",
                "source_question_id": question["id"],
                "answerable": question["answerable"],
                "reference_answer": question["reference_answer"],
                "citations": [item.model_dump(mode="json") for item in evidence],
            }
            tasks.append(FixedTask(
                task_id=spec["task_id"], task_type=spec["task_type"],
                input_text=question["query"], evidence_snapshot=json.dumps(
                    snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ), expected_evidence_refs=tuple(item.citation_id for item in evidence),
            ))
    return LoadedTaskSet(
        schema_version=manifest["schema_version"], dataset_path=str(dataset_path),
        dataset_sha256=manifest["dataset_sha256"], knowledge_snapshot_path=str(snapshot_path),
        knowledge_snapshot_sha256=manifest["knowledge_snapshot_sha256"], split=manifest["split"],
        tasks=tuple(tasks),
    )