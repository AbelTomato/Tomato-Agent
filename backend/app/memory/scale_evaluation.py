"""Synthetic SQLite scale and concurrent keyword-recall benchmark."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import tempfile
import time
from pathlib import Path

import aiosqlite

from app.memory.models import MemoryItemCreate, MemorySourceInput
from app.memory.repository import MemoryRepository
from app.memory.retrieval import MemoryRetriever
from app.memory.worker import MemoryWorker


def _latency_summary(samples: list[float]) -> dict:
    ordered = sorted(samples)
    return {
        "count": len(ordered),
        "p50_ms": round(statistics.median(ordered), 3) if ordered else None,
        "p95_ms": round(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)], 3)
        if ordered else None,
        "max_ms": round(max(ordered), 3) if ordered else None,
    }


async def _recall_once(repository: MemoryRepository, query: str, scope_id: str,
                       expected_item_id: str, *, embedding_client=None,
                       embedding_model: str = "") -> tuple[float, bool]:
    started = time.perf_counter()
    result = await MemoryRetriever(
        repository, embedding_client, embedding_model=embedding_model,
    ).recall(
        query=query, personal_scope_id=scope_id, limit=8, budget_chars=1200,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    hit = bool(result["items"] and result["items"][0]["item_id"] == expected_item_id)
    return elapsed_ms, hit


class CachedFakeEmbeddingClient:
    dimensions = 2

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


class ColdFakeEmbeddingClient:
    dimensions = 2

    def __init__(self):
        self.request_count = 0
        self.input_count = 0
        self.active_requests = 0
        self.max_active_requests = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.request_count += 1
        self.input_count += len(texts)
        self.active_requests += 1
        self.max_active_requests = max(self.max_active_requests, self.active_requests)
        try:
            await asyncio.sleep(0.02)
            return [[1.0, 0.0] if "TARGET00000" in text else [0.0, 1.0]
                    for text in texts]
        finally:
            self.active_requests -= 1


async def run_memory_scale_evaluation(*, item_count: int, serial_queries: int,
                                      concurrency_levels: list[int],
                                      embedding_mode: str = "keyword") -> dict:
    if item_count < 1 or serial_queries < 1 or not concurrency_levels or any(
        level < 1 for level in concurrency_levels
    ) or embedding_mode not in {"keyword", "cached-fake", "cold-fake", "worker-fake"}:
        raise ValueError("benchmark counts must be positive")
    with tempfile.TemporaryDirectory(prefix="tomato-memory-scale-") as temporary:
        database_path = Path(temporary) / "agent.db"
        repository = MemoryRepository(database_path)
        await repository.init()
        scope_id = "scale-evaluation-user"
        target_item_id = None
        insert_started = time.perf_counter()
        for index in range(item_count):
            marker = "TARGET00000" if index == 0 else f"FACT{index:08d}"
            fact = f"合成个人背景事实 {index:08d}，稳定标识 {marker}，用于 SQLite 召回规模测试。"
            saved = await repository.create(MemoryItemCreate(
                kind="background", scope="personal", scope_id=scope_id,
                topic_key=f"benchmark.fact.{index:08d}", content=fact,
                sources=[MemorySourceInput(
                    source_system="memory-scale-evaluation",
                    source_installation_id="synthetic-run",
                    session_id="scale-session", message_id=f"message-{index:08d}",
                    excerpt=fact,
                )],
            ), request_id=f"scale-evaluation:{index:08d}")
            if index == 0:
                target_item_id = saved.item_id
        insert_seconds = time.perf_counter() - insert_started
        assert target_item_id is not None

        embedding_client = None
        embedding_model = ""
        if embedding_mode == "cached-fake":
            embedding_model = "synthetic-two-dimensional-v1"
            items = await repository.list_items_for_recall(scope="personal", scope_id=scope_id)
            now = "2026-11-10T00:00:00+00:00"
            async with aiosqlite.connect(database_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.executemany(
                    "INSERT INTO memory_embeddings(item_id,version,model_key,dimensions,vector_json,updated_at) "
                    "VALUES(?,?,?,2,?,?)",
                    [(item.item_id, item.version, embedding_model,
                      json.dumps([1.0, 0.0] if item.item_id == target_item_id else [0.0, 1.0]), now)
                     for item in items],
                )
                await db.commit()
            embedding_client = CachedFakeEmbeddingClient()
        elif embedding_mode in {"cold-fake", "worker-fake"}:
            embedding_model = "synthetic-cold-two-dimensional-v1"
            embedding_client = ColdFakeEmbeddingClient()

        query = "TARGET00000"
        cold_concurrent = None
        background_indexing = None
        if embedding_mode == "cold-fake":
            cold_level = max(concurrency_levels)
            cold_results = await asyncio.gather(*(
                _recall_once(repository, query, scope_id, target_item_id,
                             embedding_client=embedding_client, embedding_model=embedding_model)
                for _ in range(cold_level)
            ))
            cold_concurrent = {
                **_latency_summary([elapsed for elapsed, _ in cold_results]),
                "hit_count": sum(hit for _, hit in cold_results),
                "concurrency": cold_level,
                "provider_requests": embedding_client.request_count,
                "provider_inputs": embedding_client.input_count,
                "max_provider_concurrency": embedding_client.max_active_requests,
            }
        elif embedding_mode == "worker-fake":
            worker = MemoryWorker(
                repository, None, mode="off", embedding_client=embedding_client,
                embedding_model=embedding_model,
            )
            started = time.perf_counter()
            while await worker.run_once():
                pass
            background_indexing = {
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "provider_requests": embedding_client.request_count,
                "provider_inputs": embedding_client.input_count,
                "max_provider_concurrency": embedding_client.max_active_requests,
                "remaining_missing": len(await repository.items_missing_embeddings(
                    model_key=embedding_model, limit=256,
                )),
            }
        warmup_ms, warmup_hit = await _recall_once(
            repository, query, scope_id, target_item_id,
            embedding_client=embedding_client, embedding_model=embedding_model,
        )
        serial_results = [
            await _recall_once(repository, query, scope_id, target_item_id,
                               embedding_client=embedding_client, embedding_model=embedding_model)
            for _ in range(serial_queries)
        ]
        concurrent = {}
        for level in concurrency_levels:
            concurrent_results = await asyncio.gather(*(
                _recall_once(repository, query, scope_id, target_item_id,
                             embedding_client=embedding_client, embedding_model=embedding_model)
                for _ in range(level)
            ))
            concurrent[str(level)] = {
                **_latency_summary([elapsed for elapsed, _ in concurrent_results]),
                "hit_count": sum(hit for _, hit in concurrent_results),
            }
        return {
            "schema_version": "memory-scale-evaluation/v1",
            "provider_mode": {
                "keyword": "keyword_only_no_provider",
                "cached-fake": "synthetic_cached_embedding_no_provider",
                "cold-fake": "synthetic_cold_embedding_no_provider",
                "worker-fake": "synthetic_background_embedding_no_provider",
            }[embedding_mode],
            "embedding_mode": embedding_mode,
            "cold_cache_concurrent": cold_concurrent,
            "background_indexing": background_indexing,
            "dataset": {"item_count": item_count, "scope": "personal", "source_backed": True},
            "insert": {
                "elapsed_seconds": round(insert_seconds, 3),
                "items_per_second": round(item_count / insert_seconds, 2)
                if insert_seconds else None,
            },
            "database_size_bytes": database_path.stat().st_size,
            "query": query,
            "warmup": {"latency_ms": round(warmup_ms, 3), "hit": warmup_hit},
            "serial": {
                **_latency_summary([elapsed for elapsed, _ in serial_results]),
                "hit_count": sum(hit for _, hit in serial_results),
            },
            "concurrent_by_level": concurrent,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.memory.scale_evaluation")
    parser.add_argument("--items", type=int, default=10000)
    parser.add_argument("--serial-queries", type=int, default=20)
    parser.add_argument("--concurrency-levels", type=int, nargs="+", default=[2, 4, 20])
    parser.add_argument("--embedding-mode", choices=[
        "keyword", "cached-fake", "cold-fake", "worker-fake",
    ], default="keyword")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = asyncio.run(run_memory_scale_evaluation(
        item_count=args.items, serial_queries=args.serial_queries,
        concurrency_levels=args.concurrency_levels, embedding_mode=args.embedding_mode,
    ))
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "complete", "report": str(output),
                      "serial": report["serial"],
                      "cold_cache_concurrent": report["cold_cache_concurrent"],
                      "background_indexing": report["background_indexing"],
                      "concurrent_by_level": report["concurrent_by_level"]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
