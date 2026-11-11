"""Deterministic offline evaluation for personal-memory retrieval."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
import statistics
import tempfile
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.memory.models import MemoryItemCreate, MemorySourceInput
from app.memory.repository import MemoryRepository
from app.memory.retrieval import MemoryRetriever


class MemoryEvalFact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1)
    scope: Literal["personal", "project", "session"]
    scope_id: str = Field(min_length=1)
    kind: Literal["preference", "habit", "background", "episodic"]
    topic_key: str = Field(min_length=1)
    content: str = Field(min_length=1)
    status: Literal["active", "forgotten"] = "active"


class MemoryEvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    project_scope_id: str | None = None
    session_scope_id: str | None = None
    relevant_ids: list[str] = Field(default_factory=list)
    description: str = ""


class MemoryEvalDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["memory-retrieval-dataset/v1"]
    dataset_id: str = Field(min_length=1)
    facts: list[MemoryEvalFact] = Field(min_length=1)
    cases: list[MemoryEvalCase] = Field(min_length=1)
    core_preference_ids: list[str] = Field(default_factory=list)
    supersede_previous_topic_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self):
        fact_ids = [fact.id for fact in self.facts]
        case_ids = [case.id for case in self.cases]
        if len(set(fact_ids)) != len(fact_ids):
            raise ValueError("duplicate memory fact id")
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("duplicate memory case id")
        unknown = sorted({item for case in self.cases for item in case.relevant_ids} - set(fact_ids))
        if unknown:
            raise ValueError(f"unknown relevant fact IDs: {', '.join(unknown)}")
        facts_by_id = {fact.id: fact for fact in self.facts}
        invalid_core = sorted({item for item in self.core_preference_ids if item not in facts_by_id
                               or facts_by_id[item].kind != "preference"
                               or facts_by_id[item].scope != "personal"
                               or not facts_by_id[item].topic_key.startswith(("response.", "interaction."))})
        if invalid_core:
            raise ValueError(f"invalid core preference IDs: {', '.join(invalid_core)}")
        unknown_superseding = sorted(set(self.supersede_previous_topic_ids) - set(fact_ids))
        if unknown_superseding:
            raise ValueError(f"unknown superseding fact IDs: {', '.join(unknown_superseding)}")
        return self


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class EvaluationEmbeddingBudget:
    """Bound provider requests in an explicitly selected hybrid evaluation."""

    def __init__(self, client, request_limit: int):
        if request_limit < 1:
            raise ValueError("provider request limit must be positive")
        self.client = client
        self.dimensions = client.dimensions
        self.request_limit = request_limit
        self.request_count = 0
        self.input_count = 0
        self.budget_exceeded = False

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self.request_count >= self.request_limit:
            self.budget_exceeded = True
            raise RuntimeError("memory_evaluation_provider_request_limit")
        self.request_count += 1
        self.input_count += len(texts)
        return await self.client.embed(texts)


async def run_memory_evaluation(dataset_path: Path, *, output_path: Path | None = None,
                                embedding_client=None, embedding_model: str = "",
                                min_vector_similarity: float = 0.5) -> dict:
    if embedding_client is not None and not embedding_model.strip():
        raise ValueError("embedding_model is required for hybrid memory evaluation")
    if not 0 <= min_vector_similarity <= 1:
        raise ValueError("min_vector_similarity must be between zero and one")
    dataset_path = dataset_path.expanduser().resolve(strict=True)
    if not dataset_path.is_file() or dataset_path.is_symlink():
        raise ValueError("evaluation dataset must be a regular non-symlink file")
    raw = dataset_path.read_bytes()
    dataset = MemoryEvalDataset.model_validate_json(raw)
    facts = {fact.id: fact for fact in dataset.facts}
    core_preference_ids = set(dataset.core_preference_ids) or {
        fact.id for fact in dataset.facts
        if fact.kind == "preference" and fact.scope == "personal"
        and fact.topic_key.startswith(("response.", "interaction."))
    }
    allowed_scopes = {("personal", "user-a")}
    rows = []
    durations = []
    with tempfile.TemporaryDirectory(prefix="memory-eval-") as temp_dir:
        repository = MemoryRepository(Path(temp_dir) / "memory.db")
        await repository.init()
        retriever = MemoryRetriever(
            repository, embedding_client, embedding_model=embedding_model,
            min_vector_similarity=min_vector_similarity,
        )
        repository_ids = {}
        for fact in dataset.facts:
            stored = await repository.create(MemoryItemCreate(
                kind=fact.kind, scope=fact.scope, scope_id=fact.scope_id,
                topic_key=fact.topic_key, content=fact.content,
                sources=[MemorySourceInput(
                    source_system="memory-eval", source_installation_id="fixed-v1",
                    session_id=f"source-{fact.id}", message_id=fact.id,
                    excerpt=fact.content,
                )],
            ), request_id=f"eval:{fact.id}",
                supersede_topic=fact.id in dataset.supersede_previous_topic_ids)
            repository_ids[fact.id] = stored.item_id
            if fact.status == "forgotten":
                await repository.forget(
                    stored.item_id, expected_version=stored.version,
                    request_id=f"eval-forget:{fact.id}",
                )
        labels_by_repository_id = {value: key for key, value in repository_ids.items()}

        for case in dataset.cases:
            scopes = set(allowed_scopes)
            if case.project_scope_id:
                scopes.add(("project", case.project_scope_id))
            if case.session_scope_id:
                scopes.add(("session", case.session_scope_id))
            started = time.perf_counter()
            result = await retriever.recall(
                query=case.query,
                personal_scope_id="user-a",
                project_scope_id=case.project_scope_id,
                session_scope_id=case.session_scope_id,
                limit=8,
                budget_chars=1200,
            )
            if getattr(embedding_client, "budget_exceeded", False):
                raise RuntimeError("memory_evaluation_provider_request_limit")
            duration_ms = (time.perf_counter() - started) * 1000
            durations.append(duration_ms)
            core = result.get("core_preferences", [])
            retrieved = result.get("items", [])
            selected_repository_ids = [item["item_id"] for item in core + retrieved]
            selected_ids = [labels_by_repository_id[item_id] for item_id in selected_repository_ids]
            retrieved_ids = [labels_by_repository_id[item["item_id"]] for item in retrieved]
            relevant = set(case.relevant_ids)
            hits = relevant.intersection(selected_ids)
            query_relevant = relevant - core_preference_ids
            query_hits = query_relevant.intersection(retrieved_ids)
            relevant_core_rank = next((rank for rank, item_id in enumerate(
                selected_ids[:len(core)], 1) if item_id in relevant), None)
            relevant_fact_rank = next((rank + len(core) for rank, item_id in enumerate(retrieved_ids, 1)
                                       if item_id in relevant), None)
            relevant_rank = relevant_core_rank or relevant_fact_rank
            query_fact_rank = next((rank for rank, item_id in enumerate(retrieved_ids, 1)
                                    if item_id in query_relevant), None)
            expected_context = relevant | core_preference_ids
            precision = (len(set(retrieved_ids) & relevant) / len(retrieved_ids)
                         if retrieved_ids else None)
            context_precision = (len(set(selected_ids) & expected_context) / len(selected_ids)
                                 if selected_ids else (1.0 if not expected_context else 0.0))
            recall = len(hits) / len(relevant) if relevant else None
            query_recall = len(query_hits) / len(query_relevant) if query_relevant else None
            leaks = [item_id for item_id in selected_ids
                     if (facts[item_id].scope, facts[item_id].scope_id) not in scopes]
            rows.append({
                "case_id": case.id,
                "description": case.description,
                "expected_ids": case.relevant_ids,
                "expected_core_preference_ids": sorted(core_preference_ids),
                "core_preference_ids": [labels_by_repository_id[item["item_id"]] for item in core],
                "retrieved_ids": retrieved_ids,
                "hit_ids": sorted(hits),
                "recall": recall,
                "query_fact_recall": query_recall,
                "query_fact_reciprocal_rank": 1 / query_fact_rank if query_fact_rank else (0.0 if query_relevant else None),
                "fact_precision": precision,
                "context_precision": context_precision,
                "reciprocal_rank": 1 / query_fact_rank if query_fact_rank else (0.0 if query_relevant else None),
                "context_reciprocal_rank": 1 / relevant_rank if relevant_rank else (0.0 if relevant else None),
                "expected_empty": not relevant,
                "empty_result_correct": not retrieved_ids if not relevant else None,
                "scope_leaks": leaks,
                "used_chars": result.get("used_chars", 0),
                "duration_ms": round(duration_ms, 3),
                "retrieval_mode": result.get("retrieval_mode", "keyword"),
                "vector_similarities": {
                    labels_by_repository_id.get(item_id, item_id): round(score, 6)
                    for item_id, score in getattr(retriever, "last_vector_scores", {}).items()
                },
            })

    answerable = [row for row in rows if row["expected_ids"]]
    query_answerable = [row for row in rows if row["query_fact_recall"] is not None]
    unanswerable = [row for row in rows if not row["expected_ids"]]
    recalled = [row["recall"] for row in answerable]
    fact_precisions = [row["fact_precision"] for row in rows if row["fact_precision"] is not None]
    context_precisions = [row["context_precision"] for row in rows]
    reciprocal_ranks = [row["reciprocal_rank"] for row in query_answerable]
    query_recalls = [row["query_fact_recall"] for row in query_answerable]
    query_reciprocal_ranks = [row["query_fact_reciprocal_rank"] for row in query_answerable]
    latency_sorted = sorted(durations)
    summary = {
        "case_count": len(rows),
        "answerable_case_count": len(answerable),
        "unanswerable_case_count": len(unanswerable),
        "recall_at_8": round(sum(recalled) / len(recalled), 4) if recalled else None,
        "query_fact_answerable_case_count": len(query_answerable),
        "query_fact_recall_at_8": round(sum(query_recalls) / len(query_recalls), 4)
        if query_recalls else None,
        "mean_fact_precision": round(sum(fact_precisions) / len(fact_precisions), 4)
        if fact_precisions else None,
        "mean_context_precision": round(sum(context_precisions) / len(context_precisions), 4)
        if context_precisions else None,
        "mrr": round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4) if reciprocal_ranks else None,
        "query_fact_mrr": round(sum(query_reciprocal_ranks) / len(query_reciprocal_ranks), 4)
        if query_reciprocal_ranks else None,
        "empty_result_accuracy": round(
            sum(row["empty_result_correct"] for row in unanswerable) / len(unanswerable), 4,
        ) if unanswerable else None,
        "scope_leak_count": sum(len(row["scope_leaks"]) for row in rows),
        "scope_leak_rate": round(sum(len(row["scope_leaks"]) for row in rows) / max(1, sum(
            len(row["core_preference_ids"]) + len(row["retrieved_ids"]) for row in rows
        )), 4),
        "over_budget_count": sum(row["used_chars"] > 1200 for row in rows),
        "hybrid_case_count": sum(row["retrieval_mode"] == "hybrid" for row in rows),
        "keyword_fallback_case_count": sum(row["retrieval_mode"] == "keyword" for row in rows)
        if embedding_client is not None else 0,
        "latency_ms": {
            "p50": round(statistics.median(durations), 3) if durations else None,
            "p95": round(latency_sorted[max(0, math.ceil(0.95 * len(durations)) - 1)], 3)
            if durations else None,
        },
    }
    report = {
        "schema_version": "memory-retrieval-report/v1",
        "dataset_id": dataset.dataset_id,
        "dataset_sha256": _sha256(raw),
        "retrieval_mode": "hybrid" if embedding_client is not None else "keyword",
        "embedding_model": embedding_model if embedding_client is not None else None,
        "min_vector_similarity": min_vector_similarity if embedding_client is not None else None,
        "provider_request_count": getattr(embedding_client, "request_count", None),
        "provider_input_count": getattr(embedding_client, "input_count", None),
        "provider_request_limit": getattr(embedding_client, "request_limit", None),
        "k": 8,
        "summary": summary,
        "cases": rows,
    }
    if output_path is not None:
        output_path = output_path.expanduser().resolve()
        if output_path == dataset_path:
            raise ValueError("report output must not overwrite evaluation dataset")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.memory.evaluation")
    parser.add_argument("--dataset", type=Path, default=Path(__file__).resolve().parents[2] / "evals" / "memory_retrieval_v2.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mode", choices=("keyword", "hybrid"), default="keyword")
    parser.add_argument("--provider-request-limit", type=int)
    parser.add_argument("--min-vector-similarity", type=float, default=0.5)
    args = parser.parse_args(argv)
    embedding_client = None
    embedding_model = ""
    if args.mode == "hybrid":
        if args.provider_request_limit is None:
            parser.error("--mode hybrid requires --provider-request-limit")
        from app.knowledge.pipeline_factory import (
            create_embedding_client, runtime_config_from_settings,
        )
        from app.settings import Settings

        config = runtime_config_from_settings(Settings())
        embedding_client = create_embedding_client(config)
        if embedding_client is None:
            parser.error("hybrid evaluation requires configured embedding credentials and model")
        embedding_model = f"{config.embedding_model}|{config.embedding_dimensions}"
        embedding_client = EvaluationEmbeddingBudget(embedding_client, args.provider_request_limit)
    elif args.provider_request_limit is not None:
        parser.error("--provider-request-limit is only valid with --mode hybrid")
    report = asyncio.run(run_memory_evaluation(
        args.dataset, output_path=args.output, embedding_client=embedding_client,
        embedding_model=embedding_model, min_vector_similarity=args.min_vector_similarity,
    ))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
