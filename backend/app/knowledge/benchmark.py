"""Read-only retrieval benchmark runner and command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import time
import uuid
from pathlib import Path
from typing import Callable, Sequence

from app.knowledge.benchmark_compare import compare_reports, write_comparison
from app.knowledge.benchmark_evaluation import evaluate_questions
from app.knowledge.benchmark_models import BenchmarkManifest, RunReport, StrategyConfig
from app.knowledge.benchmark_snapshot import (
    _canonical_hash,
    _read_jsonl,
    fingerprint_knowledge_database,
    load_questions,
    open_read_only_database,
)
from app.knowledge.embeddings import EmbeddingClient, EmbeddingProvider
from app.knowledge.models import SearchResult
from app.knowledge.repository import KnowledgeRepository
from app.settings import settings


SearchOverride = Callable[[str, int], Sequence[SearchResult]]
METRIC_VERSION = "benchmark-metrics/v1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_fingerprint() -> str:
    root = Path(__file__).resolve().parents[2]
    files = [
        root / "app/knowledge/benchmark.py",
        root / "app/knowledge/benchmark_compare.py",
        root / "app/knowledge/benchmark_evaluation.py",
        root / "app/knowledge/benchmark_models.py",
        root / "app/knowledge/benchmark_snapshot.py",
        root / "app/knowledge/candidate_retrieval.py",
        root / "app/knowledge/retrieval.py",
    ]
    return _canonical_hash({path.name: _sha256(path) for path in sorted(files)})


def _score_type(mode: str) -> str:
    return {"keyword": "bm25", "vector": "cosine", "hybrid": "rrf"}[mode]


async def run_benchmark(
    manifest: BenchmarkManifest,
    *,
    query_embedder: EmbeddingProvider | None = None,
    search_override: SearchOverride | None = None,
) -> RunReport:
    dataset_path = Path(manifest.dataset).expanduser().resolve()
    database_path = Path(manifest.database).expanduser().resolve()
    connection = open_read_only_database(database_path)
    try:
        documents = connection.execute(
            "SELECT document_id, source_path, content_hash FROM documents ORDER BY document_id"
        ).fetchall()
        line_counts: dict[str, int] = {}
        for row in connection.execute("SELECT document_id, MAX(end_line) FROM chunks GROUP BY document_id"):
            line_counts[row[0]] = int(row[1] or 0)
    finally:
        connection.close()

    questions = load_questions(dataset_path, split=manifest.split, document_line_counts=line_counts)
    if manifest.question_ids and set(manifest.question_ids) != {item["id"] for item in questions}:
        raise ValueError("manifest question IDs do not match selected split")
    dataset_fingerprint = _canonical_hash(list(questions))
    index_fingerprint = fingerprint_knowledge_database(database_path)
    corpus_fingerprint = _canonical_hash([list(row) for row in documents])
    source_fingerprint = _source_fingerprint()
    config_fingerprint = _canonical_hash(manifest.strategy.model_dump(mode="json"))

    strategy = manifest.strategy
    if search_override is None:
        if strategy.mode in {"vector", "hybrid"} and query_embedder is None:
            raise ValueError(f"{strategy.mode} benchmark requires an injected query embedder")
        if query_embedder is None or isinstance(query_embedder, EmbeddingClient):
            if strategy.parameters.get("embedding_model") and strategy.parameters["embedding_model"] != settings.embedding_model:
                raise ValueError("configured embedding model does not match manifest")
            if strategy.parameters.get("embedding_dimensions") and strategy.parameters["embedding_dimensions"] != settings.embedding_dimensions:
                raise ValueError("configured embedding dimensions do not match manifest")

        repository = KnowledgeRepository(database_path, read_only=True)
        await repository.init()

        async def search(query: str, limit: int) -> list[SearchResult]:
            if strategy.mode == "keyword":
                return await repository.search_chunks(query, limit=limit)
            vectors = await query_embedder.embed([query])  # type: ignore[union-attr]
            vector = vectors[0]
            model = str(strategy.parameters.get("embedding_model", settings.embedding_model))
            dimensions = int(strategy.parameters.get("embedding_dimensions", settings.embedding_dimensions))
            if strategy.mode == "vector":
                return await repository.search_vector_chunks(
                    vector, model=model, dimensions=dimensions, limit=limit,
                    min_score=strategy.similarity_threshold,
                )
            return await repository.search_hybrid_chunks(
                query, vector, model=model, dimensions=dimensions, limit=limit,
                min_vector_score=strategy.similarity_threshold,
            )
    else:
        async def search(query: str, limit: int) -> list[SearchResult]:
            value = search_override(query, limit)
            if asyncio.iscoroutine(value):
                return await value
            return list(value)

    result_map: dict[str, Sequence[SearchResult] | BaseException] = {}
    durations: dict[str, float] = {}
    for question in questions:
        started = time.perf_counter()
        try:
            result_map[question["id"]] = await search(
                question["query"], min(strategy.top_k, strategy.final_limit)
            )
        except Exception as exc:
            result_map[question["id"]] = exc
        durations[question["id"]] = (time.perf_counter() - started) * 1000

    evaluated, summary_model = evaluate_questions(questions, result_map, score_type=_score_type(strategy.mode))
    results = tuple(
        item.model_copy(update={"duration_ms": durations[item.question_id]})
        for item in evaluated
    )
    summary = summary_model.model_dump(mode="json")
    latency = [value for question_id, value in durations.items() if not isinstance(result_map[question_id], BaseException)]
    summary["latency_ms"] = {
        "p50": statistics.median(latency) if latency else None,
        "p95": sorted(latency)[max(0, int(0.95 * len(latency) + 0.999) - 1)] if latency else None,
        "max": max(latency) if latency else None,
        "sample_count": len(latency),
    }
    return RunReport(
        schema_version="benchmark-run/v1",
        run_id=str(uuid.uuid4()),
        manifest=manifest,
        strategy=strategy,
        dataset_sha256=dataset_fingerprint,
        corpus_sha256=corpus_fingerprint,
        index_sha256=index_fingerprint,
        source_sha256=source_fingerprint,
        metric_version=METRIC_VERSION,
        status="incomplete" if summary_model.error_count else "complete",
        questions=results,
        expected_question_ids=tuple(item["id"] for item in questions),
        summary=summary,
    )


def write_run_report(report: RunReport, output_dir: Path) -> tuple[Path, Path]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    json_path = output_dir / "report.json"
    markdown_path = output_dir / "report.md"
    json_path.write_text(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Benchmark Run 报告",
        "",
        f"- Run ID：`{report.run_id}`",
        f"- 状态：`{report.status}`",
        f"- 策略：`{report.strategy.name}`（`{report.strategy.mode}`）",
        f"- Split：`{report.manifest.split}`",
        f"- 指标版本：`{report.metric_version}`",
        "",
        "| 指标 | 数值 | 样本数 |",
        "|---|---:|---:|",
    ]
    for key, value in report.summary.items():
        if isinstance(value, dict) and "sample_count" in value:
            lines.append(f"| {key} | {value.get('value')} | {value['sample_count']} |")
    lines.extend(["", "## 逐题结果", "", "| 题目 | 状态 | Recall@5 | RR@5 | 检索数 | 延迟 ms |", "|---|---|---:|---:|---:|---:|"])
    lines.extend(
        f"| {item.question_id} | {item.status} | {item.recall} | {item.reciprocal_rank} | {len(item.retrieved_chunk_ids)} | {item.duration_ms:.3f} |"
        for item in report.questions
    )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path


def _load_manifest(path: Path) -> BenchmarkManifest:
    return BenchmarkManifest.model_validate_json(path.read_text(encoding="utf-8"))


async def _run_cli(args: argparse.Namespace) -> int:
    if args.command == "run":
        manifest = _load_manifest(args.manifest)
        if args.strategy is not None and args.strategy != manifest.strategy.name:
            raise ValueError("--strategy must match the strategy name in the manifest")
        if args.split is not None and args.split != manifest.split:
            raise ValueError("--split must match the split in the manifest")
        requested_mode = getattr(args, "mode", None)
        if requested_mode is not None:
            mode = requested_mode
            strategy = manifest.strategy.model_copy(
                update={
                    "mode": mode,
                    "name": f"{mode}-v1",
                    "similarity_threshold": (
                        settings.knowledge_min_vector_similarity if mode == "vector"
                        else settings.knowledge_candidate_min_vector_similarity if mode == "hybrid"
                        else 0.0
                    ),
                    "parameters": (
                        {
                            "score_type": "cosine" if mode == "vector" else "rrf",
                            "embedding_model": settings.embedding_model,
                            "embedding_dimensions": settings.embedding_dimensions,
                        }
                        if mode in {"vector", "hybrid"}
                        else {"score_type": "bm25"}
                    ),
                }
            )
            manifest = manifest.model_copy(update={"strategy": strategy})
        query_embedder = getattr(args, "query_embedder", None)
        if manifest.strategy.mode in {"vector", "hybrid"} and query_embedder is None:
            if not settings.embedding_api_key.strip():
                raise ValueError("vector/hybrid benchmark requires EMBEDDING_API_KEY")
            if not settings.embedding_model.strip() or settings.embedding_dimensions <= 0:
                raise ValueError("vector/hybrid benchmark requires valid embedding model and dimensions")
            query_embedder = EmbeddingClient(
                api_key=settings.embedding_api_key,
                base_url=settings.embedding_base_url,
                model=settings.embedding_model,
                dimensions=settings.embedding_dimensions,
                timeout_seconds=settings.embedding_timeout_seconds,
            )
        report = await run_benchmark(manifest, query_embedder=query_embedder)
        write_run_report(report, args.output)
        return 1 if report.status != "complete" else 0
    baseline = RunReport.model_validate_json(args.baseline.read_text(encoding="utf-8"))
    candidate = RunReport.model_validate_json(args.candidate.read_text(encoding="utf-8"))
    comparison = compare_reports(baseline, candidate)
    write_comparison(comparison, args.output)
    return 1 if comparison.status in {"incomplete", "incomparable"} else 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.knowledge.benchmark")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--manifest", type=Path, required=True)
    run_parser.add_argument("--strategy", help="optional assertion against the manifest strategy name")
    run_parser.add_argument("--mode", choices=("keyword", "vector", "hybrid"), help="optional retrieval mode override; vector/hybrid use configured Embedding service")
    run_parser.add_argument("--split", choices=("smoke", "dev", "test"), help="optional assertion against the manifest split")
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.set_defaults(query_embedder=None)
    compare_parser = commands.add_parser("compare")
    compare_parser.add_argument("--baseline", type=Path, required=True)
    compare_parser.add_argument("--candidate", type=Path, required=True)
    compare_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        return asyncio.run(_run_cli(args))
    except Exception as exc:
        parser.exit(2, f"benchmark error: {exc}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())