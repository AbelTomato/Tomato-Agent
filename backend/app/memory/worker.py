"""Lifecycle-managed worker for memory extraction, projections, and embeddings."""

import asyncio
import logging
import time

from app.memory.extraction import MemoryExtractor, _SENSITIVE
from app.memory.models import MemoryItemCreate, MemorySourceInput
from app.memory.projection import MemoryProjectionBuilder

logger = logging.getLogger(__name__)


class MemoryWorker:
    def __init__(self, repository, extractor: MemoryExtractor | None, *, mode: str = "automatic",
                 projection_builder: MemoryProjectionBuilder | None = None,
                 embedding_client=None, embedding_model: str = "",
                 poll_interval_seconds: float = 1.0):
        if mode not in {"off", "automatic", "review"} or poll_interval_seconds <= 0:
            raise ValueError("invalid_memory_worker_config")
        self.repository = repository
        self.extractor = extractor
        self.projection_builder = projection_builder
        self.embedding_client = embedding_client
        self.embedding_model = embedding_model
        self.mode = mode
        self.poll_interval_seconds = poll_interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._embedding_failures = 0
        self._embedding_retry_after = 0.0

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._run(), name="memory-maintenance-worker")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop.set()
        await task
        self._task = None

    async def run_once(self) -> bool:
        job = await self.repository.claim_job() if self.extractor and self.mode != "off" else None
        if job is None:
            if await self._run_projection_once():
                return True
            return await self._run_embedding_once()
        try:
            payload = job["payload"]
            if await self.repository.is_source_deleted(payload):
                await self.repository.finish_job(job["job_id"])
                return True
            result = await self.extractor.extract(payload["messages"])
            messages = {item["message_id"]: item for item in payload["messages"]}
            for fact in result.facts:
                evidence = [messages.get(message_id) for message_id in fact.evidence_message_ids]
                if not evidence or any(item is None for item in evidence):
                    continue
                evidence = [item for item in evidence if item["role"] == "user" and item["content"].strip()]
                if (not evidence or _SENSITIVE.search(fact.content)
                        or any(_SENSITIVE.search(item["content"]) for item in evidence)):
                    continue
                sources = [MemorySourceInput(
                    source_system=payload["source_system"],
                    source_installation_id=payload["source_installation_id"],
                    session_id=payload["session_id"],
                    message_id=item["message_id"],
                    source_revision=item["revision"],
                    role="user",
                    message_at=item.get("message_at"),
                    excerpt=item["content"][:1000],
                ) for item in evidence]
                existing = await self.repository.active_for_topic(job["scope_id"], fact.topic_key)
                conflict = any(item.content.strip().casefold() != fact.content.strip().casefold()
                               for item in existing)
                eligible = self.extractor.auto_eligible(
                    fact, "\n".join(item["content"] for item in evidence),
                )
                if self.mode == "automatic" and eligible and not conflict:
                    if any(item.content.strip().casefold() == fact.content.strip().casefold()
                           for item in existing):
                        continue
                    await self.repository.create(MemoryItemCreate(
                        kind=fact.kind, scope="personal", scope_id=job["scope_id"],
                        topic_key=fact.topic_key, content=fact.content.strip(),
                        confirmation="automatic", sources=sources,
                    ), request_id=f"extract:{job['job_id']}:{fact.topic_key}")
                elif fact.confidence >= 0.55:
                    await self.repository.add_candidate(scope_id=job["scope_id"], payload={
                        "kind": fact.kind,
                        "topic_key": fact.topic_key,
                        "content": fact.content.strip(),
                        "confidence": fact.confidence,
                        "reason": "conflict" if conflict else "needs_review",
                        "conflicting_item_ids": [item.item_id for item in existing],
                        "sources": [source.model_dump() for source in sources],
                    })
            await self.repository.finish_job(job["job_id"])
        except Exception as exc:
            logger.warning("Memory extraction job failed job_id=%s error=%s", job["job_id"], type(exc).__name__)
            await self.repository.finish_job(job["job_id"], error=type(exc).__name__, retry=job["attempts"] < 3)
        return True

    async def _run_projection_once(self) -> bool:
        if self.projection_builder is None:
            return False
        job = await self.repository.claim_projection_job()
        if job is None:
            return False
        try:
            await self.projection_builder.refresh_profile(job)
            await self.repository.finish_projection_job(job["job_id"])
        except Exception as exc:
            logger.warning("Memory projection job failed job_id=%s error=%s",
                           job["job_id"], type(exc).__name__)
            await self.repository.finish_projection_job(
                job["job_id"], error=type(exc).__name__, retry=job["attempts"] < 3,
            )
        return True

    async def _run_embedding_once(self) -> bool:
        if self.embedding_client is None or not self.embedding_model:
            return False
        state = await self.repository.embedding_backfill_state(model_key=self.embedding_model)
        self._embedding_failures = max(self._embedding_failures, state["failures"])
        self._embedding_retry_after = float(state["retry_at"])
        if time.time() < self._embedding_retry_after:
            return False
        try:
            semaphore = self.repository.embedding_backfill_semaphore(self.embedding_model)
            async with semaphore:
                items = await self.repository.items_missing_embeddings(
                    model_key=self.embedding_model, limit=64,
                )
                if not items:
                    if state["failures"]:
                        await self.repository.clear_embedding_backfill_failure(
                            model_key=self.embedding_model,
                        )
                    self._embedding_failures = 0
                    self._embedding_retry_after = 0.0
                    return False
                vectors = await self.embedding_client.embed([
                    f"{item['topic_key']}: {item['content']}" for item in items
                ])
                dimensions = getattr(self.embedding_client, "dimensions", None)
                if len(vectors) != len(items) or not vectors:
                    raise ValueError("memory_embedding_count_mismatch")
                dimensions = dimensions or len(vectors[0])
                if any(len(vector) != dimensions for vector in vectors):
                    raise ValueError("memory_embedding_dimension_mismatch")
                await self.repository.store_embeddings(
                    model_key=self.embedding_model,
                    dimensions=dimensions,
                    embeddings=[(item["item_id"], item["version"], vector)
                                for item, vector in zip(items, vectors)],
                )
            self._embedding_failures = 0
            self._embedding_retry_after = 0.0
            if state["failures"]:
                await self.repository.clear_embedding_backfill_failure(model_key=self.embedding_model)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._embedding_failures += 1
            delay = min(60.0, 2 ** min(self._embedding_failures - 1, 6))
            self._embedding_retry_after = time.time() + delay
            await self.repository.record_embedding_backfill_failure(
                model_key=self.embedding_model,
                failures=self._embedding_failures,
                retry_at=int(self._embedding_retry_after),
            )
            logger.warning(
                "Memory embedding backfill failed model=%s retry_seconds=%s error=%s",
                self.embedding_model, delay, type(exc).__name__,
            )
            return False

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                worked = await self.run_once()
            except Exception:
                logger.exception("Memory maintenance worker iteration failed")
                worked = False
            if worked:
                continue
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass
