from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import argparse
from pathlib import Path

import pytest

from app.knowledge.benchmark import _run_cli, run_benchmark, write_run_report
from app.knowledge.benchmark_compare import compare_reports, write_comparison
from app.knowledge.benchmark_models import BenchmarkManifest
from app.knowledge.embeddings import EmbeddingClient


def make_snapshot(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE documents (
                document_id TEXT PRIMARY KEY, source_path TEXT, source_url TEXT,
                title TEXT, content_hash TEXT, index_status TEXT, index_error TEXT
            );
            CREATE TABLE chunks (
                chunk_id TEXT PRIMARY KEY, document_id TEXT, document_version TEXT,
                heading_path TEXT, start_line INTEGER, end_line INTEGER,
                text TEXT, token_count INTEGER
            );
            CREATE TABLE embeddings (
                chunk_id TEXT, document_id TEXT, document_version TEXT,
                model TEXT, dimensions INTEGER, text_hash TEXT, vector_json TEXT
            );
            INSERT INTO documents VALUES ('doc', 'doc.md', NULL, 'Doc', 'hash', 'ready', NULL);
            INSERT INTO chunks VALUES ('chunk-hit', 'doc', 'v1', 'Terms', 1, 2, 'alpha beta', 2);
            INSERT INTO chunks VALUES ('chunk-miss', 'doc', 'v1', 'Other', 5, 6, 'gamma', 1);
            INSERT INTO embeddings VALUES ('chunk-hit', 'doc', 'v1', 'fake-embedding', 2, 'hash-hit', '[1,0]');
            INSERT INTO embeddings VALUES ('chunk-miss', 'doc', 'v1', 'fake-embedding', 2, 'hash-miss', '[0,1]');
            """
        )


def write_dataset(path: Path) -> None:
    questions = [
        {
            "id": "q-dev", "group_id": "g-dev", "query": "alpha", "category": "term",
            "split": "dev", "answerable": True,
            "relevant_spans": [{"document_id": "doc", "document_version": "v1",
                                "start_line": 1, "end_line": 2}],
            "reference_answer": "alpha",
        },
        {
            "id": "q-test", "group_id": "g-test", "query": "gamma", "category": "term",
            "split": "test", "answerable": False, "relevant_spans": [],
            "reference_answer": "refuse",
        },
    ]
    path.write_text("\n".join(json.dumps(item) for item in questions), encoding="utf-8")


def write_manifest(path: Path, dataset: Path, database: Path) -> BenchmarkManifest:
    value = {
        "schema_version": "benchmark-manifest/v1",
        "dataset": str(dataset),
        "split": "dev",
        "database": str(database),
        "comparison_scope": "fixed_index",
        "allowed_changes": ["retrieval_mode", "similarity_threshold"],
        "strategy": {
            "name": "keyword-v1", "mode": "keyword", "top_k": 5,
            "candidate_limit": 5, "final_limit": 5, "similarity_threshold": 0.5,
        },
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return BenchmarkManifest.model_validate(value)


class FakeEmbedder:
    def __init__(self, *, fail_query: str | None = None) -> None:
        self.fail_query = fail_query
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if self.fail_query and self.fail_query in texts[0]:
            raise RuntimeError("fake embedding failure")
        return [[1.0, 0.0] for _ in texts]


def manifest_for_mode(path: Path, dataset: Path, database: Path, mode: str) -> BenchmarkManifest:
    value = {
        "schema_version": "benchmark-manifest/v1",
        "dataset": str(dataset),
        "split": "dev",
        "database": str(database),
        "comparison_scope": "fixed_index",
        "allowed_changes": ["retrieval_mode", "similarity_threshold"],
        "strategy": {
            "name": f"{mode}-v1", "mode": mode, "top_k": 5,
            "candidate_limit": 5, "final_limit": 5, "similarity_threshold": 0.5,
            "parameters": {"embedding_model": "fake-embedding", "embedding_dimensions": 2},
        },
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return BenchmarkManifest.model_validate(value)


@pytest.mark.asyncio
async def test_runner_reads_only_selected_split_and_snapshot_read_only(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    database = tmp_path / "snapshot.db"
    write_dataset(dataset)
    make_snapshot(database)
    manifest = write_manifest(tmp_path / "manifest.json", dataset, database)
    before = database.read_bytes()

    report = await run_benchmark(manifest)

    assert report.status == "complete"
    assert [item.question_id for item in report.questions] == ["q-dev"]
    assert report.questions[0].recall == 1.0
    assert database.read_bytes() == before


@pytest.mark.asyncio
async def test_runner_records_per_question_failure_without_calling_it_empty(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    database = tmp_path / "snapshot.db"
    write_dataset(dataset)
    make_snapshot(database)
    manifest = write_manifest(tmp_path / "manifest.json", dataset, database)

    report = await run_benchmark(manifest, search_override=lambda *_: (_ for _ in ()).throw(RuntimeError("offline")))

    assert report.status == "incomplete"
    assert report.questions[0].status == "error"
    assert report.questions[0].error_code == "RuntimeError"
    assert report.summary["error_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["vector", "hybrid"])
async def test_runner_uses_injected_embedder_for_vector_modes(tmp_path, mode):
    dataset = tmp_path / "questions.jsonl"
    database = tmp_path / "snapshot.db"
    write_dataset(dataset)
    make_snapshot(database)
    manifest = manifest_for_mode(tmp_path / "manifest.json", dataset, database, mode)
    embedder = FakeEmbedder()

    report = await run_benchmark(manifest, query_embedder=embedder)

    assert report.status == "complete"
    assert len(embedder.calls) == 1
    assert embedder.calls[0] == ["alpha"]
    assert report.questions[0].score_type == ("cosine" if mode == "vector" else "rrf")


@pytest.mark.asyncio
async def test_runner_keeps_embedding_failure_as_incomplete_question(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    database = tmp_path / "snapshot.db"
    write_dataset(dataset)
    make_snapshot(database)
    manifest = manifest_for_mode(tmp_path / "manifest.json", dataset, database, "vector")

    report = await run_benchmark(
        manifest,
        query_embedder=FakeEmbedder(fail_query="alpha"),
    )

    assert report.status == "incomplete"
    assert report.questions[0].status == "error"
    assert report.questions[0].error_code == "RuntimeError"


@pytest.mark.asyncio
async def test_cli_writes_incomplete_report_and_returns_nonzero(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    database = tmp_path / "snapshot.db"
    write_dataset(dataset)
    make_snapshot(database)
    manifest = manifest_for_mode(tmp_path / "manifest.json", dataset, database, "vector")
    output = tmp_path / "failed-run"
    args = argparse.Namespace(
        command="run",
        manifest=tmp_path / "manifest.json",
        strategy=None,
        mode=None,
        split=None,
        output=output,
        query_embedder=FakeEmbedder(fail_query="alpha"),
    )

    exit_code = await _run_cli(args)
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))

    assert exit_code == 1
    assert report["status"] == "incomplete"
    assert report["questions"][0]["status"] == "error"
    assert report["questions"][0]["error_code"] == "RuntimeError"


@pytest.mark.asyncio
async def test_cli_mode_override_is_recorded_in_run_report(tmp_path, monkeypatch):
    dataset = tmp_path / "questions.jsonl"
    database = tmp_path / "snapshot.db"
    write_dataset(dataset)
    make_snapshot(database)
    manifest_for_mode(tmp_path / "manifest.json", dataset, database, "keyword")
    monkeypatch.setattr("app.knowledge.benchmark.settings.embedding_api_key", "test-key")
    monkeypatch.setattr("app.knowledge.benchmark.settings.embedding_model", "fake-embedding")
    monkeypatch.setattr("app.knowledge.benchmark.settings.embedding_dimensions", 2)
    args = argparse.Namespace(
        command="run",
        manifest=tmp_path / "manifest.json",
        strategy=None,
        mode="vector",
        split="dev",
        output=tmp_path / "vector-run",
        query_embedder=FakeEmbedder(),
    )

    exit_code = await _run_cli(args)
    report = json.loads((args.output / "report.json").read_text(encoding="utf-8"))

    assert exit_code == 0
    assert report["strategy"]["mode"] == "vector"
    assert report["strategy"]["parameters"]["embedding_model"] == "fake-embedding"


@pytest.mark.asyncio
async def test_run_report_writer_refuses_to_overwrite_existing_directory(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    database = tmp_path / "snapshot.db"
    write_dataset(dataset)
    make_snapshot(database)
    manifest = write_manifest(tmp_path / "manifest.json", dataset, database)
    report = await run_benchmark(manifest)

    output = tmp_path / "run"
    output.mkdir()
    with pytest.raises(FileExistsError):
        write_run_report(report, output)


@pytest.mark.asyncio
async def test_run_compare_integration_is_reproducible_and_preserves_snapshot(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    database = tmp_path / "snapshot.db"
    write_dataset(dataset)
    make_snapshot(database)
    manifest = write_manifest(tmp_path / "manifest.json", dataset, database)
    before = database.read_bytes()

    baseline = await run_benchmark(manifest)
    candidate = await run_benchmark(manifest)
    comparison = compare_reports(baseline, candidate)
    run_json, run_markdown = write_run_report(baseline, tmp_path / "baseline")
    comparison_json, comparison_markdown = write_comparison(comparison, tmp_path / "comparison")

    stable_metrics = set(baseline.summary) - {"latency_ms"}
    assert {key: baseline.summary[key] for key in stable_metrics} == {
        key: candidate.summary[key] for key in stable_metrics
    }
    assert comparison.status == "unchanged"
    assert json.loads(run_json.read_text(encoding="utf-8"))["summary"] == baseline.summary
    assert "Recall@5" in run_markdown.read_text(encoding="utf-8")
    comparison_payload = json.loads(comparison_json.read_text(encoding="utf-8"))
    assert comparison_payload["status"] == "unchanged"
    assert "unchanged" in comparison_markdown.read_text(encoding="utf-8")
    assert database.read_bytes() == before


def test_cli_rejects_strategy_or_split_that_disagrees_with_manifest(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    database = tmp_path / "snapshot.db"
    write_dataset(dataset)
    make_snapshot(database)
    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest_path, dataset, database)

    result = subprocess.run(
        [
            sys.executable, "-m", "app.knowledge.benchmark", "run",
            "--manifest", str(manifest_path), "--strategy", "other",
            "--split", "dev", "--output", str(tmp_path / "run"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "--strategy must match" in result.stderr
    assert not (tmp_path / "run").exists()

    split_result = subprocess.run(
        [
            sys.executable, "-m", "app.knowledge.benchmark", "run",
            "--manifest", str(manifest_path), "--split", "test",
            "--output", str(tmp_path / "split-run"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert split_result.returncode == 2
    assert "--split must match" in split_result.stderr
    assert not (tmp_path / "split-run").exists()


def test_cli_run_writes_report_and_refuses_to_overwrite(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    database = tmp_path / "snapshot.db"
    write_dataset(dataset)
    make_snapshot(database)
    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest_path, dataset, database)
    output = tmp_path / "run"
    command = [
        sys.executable, "-m", "app.knowledge.benchmark", "run",
        "--manifest", str(manifest_path), "--output", str(output),
    ]

    result = subprocess.run(command, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    markdown = (output / "report.md").read_text(encoding="utf-8")
    assert report["status"] == "complete"
    assert "Recall@5" in markdown
    original = (output / "report.json").read_bytes()

    overwrite_result = subprocess.run(command, capture_output=True, text=True, check=False)

    assert overwrite_result.returncode == 2
    assert "File exists" in overwrite_result.stderr
    assert (output / "report.json").read_bytes() == original


def test_cli_records_failed_question_and_returns_nonzero(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    database = tmp_path / "snapshot.db"
    write_dataset(dataset)
    make_snapshot(database)
    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest_path, dataset, database)
    failing_database = tmp_path / "missing-snapshot.db"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["database"] = str(failing_database)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "failed-run"

    result = subprocess.run(
        [
            sys.executable, "-m", "app.knowledge.benchmark", "run",
            "--manifest", str(manifest_path), "--output", str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert not output.exists()


@pytest.mark.asyncio
async def test_cli_compare_writes_json_and_markdown(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    database = tmp_path / "snapshot.db"
    write_dataset(dataset)
    make_snapshot(database)
    manifest = write_manifest(tmp_path / "manifest.json", dataset, database)
    baseline = await run_benchmark(manifest)
    candidate = await run_benchmark(manifest)
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    baseline_path.write_text(baseline.model_dump_json(), encoding="utf-8")
    candidate_path.write_text(candidate.model_dump_json(), encoding="utf-8")
    output = tmp_path / "comparison"

    result = subprocess.run(
        [
            sys.executable, "-m", "app.knowledge.benchmark", "compare",
            "--baseline", str(baseline_path), "--candidate", str(candidate_path),
            "--output", str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    comparison = json.loads((output / "comparison.json").read_text(encoding="utf-8"))
    markdown = (output / "comparison.md").read_text(encoding="utf-8")
    assert comparison["status"] == "unchanged"
    assert "| recall_at_5 |" in markdown
    assert "| mrr_at_5 |" in markdown
    assert "unchanged" in markdown