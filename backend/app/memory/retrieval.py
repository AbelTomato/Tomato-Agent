"""Scope-filtered keyword and optional vector retrieval for short memory facts."""

import asyncio
import math
import re



_LATIN = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_CJK = re.compile(r"[\u3400-\u9fff]+")
_STOP = {"这个", "那个", "什么", "怎么", "可以", "一下", "一下子", "现在", "之前", "以后", "回答"}
_MIN_LEXICAL_SCORE = 0.06
_MIN_VECTOR_SIMILARITY = 0.5
_ON_DEMAND_EMBED_ITEM_LIMIT = 64


def _terms(value: str) -> set[str]:
    lowered = value.casefold()
    terms = set(_LATIN.findall(lowered))
    for run in _CJK.findall(lowered):
        if len(run) == 1:
            terms.add(run)
        else:
            terms.update(run[index:index + 2] for index in range(len(run) - 1))
    return terms - _STOP


class MemoryRetriever:
    def __init__(self, repository, embedding_client=None, *, embedding_model: str = "",
                 min_vector_similarity: float = _MIN_VECTOR_SIMILARITY):
        if not 0 <= min_vector_similarity <= 1:
            raise ValueError("min_vector_similarity must be between zero and one")
        self.repository = repository
        self.embedding_client = embedding_client
        self.embedding_model = embedding_model
        self.min_vector_similarity = min_vector_similarity
        self.last_vector_scores: dict[str, float] = {}

    async def recall(self, *, query: str, personal_scope_id: str,
                     project_scope_id: str | None = None,
                     session_scope_id: str | None = None,
                     limit: int = 8, budget_chars: int = 1200) -> dict:
        semaphore = getattr(self.repository, "keyword_recall_semaphore", None)
        if self.embedding_client is None and semaphore is not None:
            async with semaphore:
                return await self._recall(
                    query=query, personal_scope_id=personal_scope_id,
                    project_scope_id=project_scope_id,
                    session_scope_id=session_scope_id, limit=limit,
                    budget_chars=budget_chars,
                )
        vector_semaphore = getattr(self.repository, "cached_vector_recall_semaphore", None)
        if self.embedding_client is not None and vector_semaphore is not None:
            async with vector_semaphore:
                return await self._recall(
                    query=query, personal_scope_id=personal_scope_id,
                    project_scope_id=project_scope_id,
                    session_scope_id=session_scope_id, limit=limit,
                    budget_chars=budget_chars, vector_semaphore=vector_semaphore,
                )
        return await self._recall(
            query=query, personal_scope_id=personal_scope_id,
            project_scope_id=project_scope_id,
            session_scope_id=session_scope_id, limit=limit,
            budget_chars=budget_chars,
        )

    async def _recall(self, *, query: str, personal_scope_id: str,
                      project_scope_id: str | None = None,
                      session_scope_id: str | None = None,
                      limit: int = 8, budget_chars: int = 1200,
                      vector_semaphore=None) -> dict:
        query = query.strip()
        self.last_vector_scores = {}
        if not query:
            return {"generation": await self.repository.generation(), "items": [], "used_chars": 0,
                    "retrieval_mode": "keyword"}
        generation = await self.repository.generation()
        query_terms = _terms(query)
        scopes = [("personal", personal_scope_id)]
        if project_scope_id:
            scopes.append(("project", project_scope_id))
        if session_scope_id:
            scopes.append(("session", session_scope_id))

        scoped_items = []
        for scope, scope_id in scopes:
            scoped_items.extend(await self.repository.list_items_for_recall(
                scope=scope, scope_id=scope_id,
            ))

        core_preferences = []
        preference_chars = 0
        seen_preferences = set()
        source_cache = await self.repository.sources_for_recall(
            [(item.item_id, item.version) for item in scoped_items]
        )
        for item in scoped_items:
            sources = [source for source in source_cache.get((item.item_id, item.version), [])
                       if source["status"] == "active"]
            source_cache[item.item_id] = sources
            if (item.kind != "preference" or not item.topic_key.startswith(("response.", "interaction."))
                    or item.item_id in seen_preferences or not sources):
                continue
            cost = len(item.content.strip()) + 48
            if preference_chars + cost > min(300, budget_chars // 3):
                continue
            seen_preferences.add(item.item_id)
            core_preferences.append({
                "item_id": item.item_id,
                "version": item.version,
                "topic_key": item.topic_key,
                "content": item.content.strip(),
                "scope": item.scope,
                "scope_id": item.scope_id,
                "sources": [{
                    "session_id": source["session_id"],
                    "message_id": source["message_id"],
                    "source_revision": source["source_revision"],
                } for source in sources],
            })
            preference_chars += cost

        ranked = []
        for item in scoped_items:
            sources = source_cache[item.item_id]
            if item.item_id in seen_preferences or not sources:
                continue
            content_terms = _terms(item.content)
            topic_terms = _terms(item.topic_key.replace(".", " ").replace("_", " "))
            overlap = len(query_terms & content_terms)
            topic_overlap = len(query_terms & topic_terms)
            score = overlap / max(1, len(query_terms)) + topic_overlap * 0.5
            if item.kind == "preference":
                score += 0.15
            if item.content.casefold() in query.casefold() or query.casefold() in item.content.casefold():
                score += 1.0
            ranked.append((score, item, sources))
        lexical_order = sorted(
            (row for row in ranked
             if (row[0] >= _MIN_LEXICAL_SCORE and (
                 len(query_terms & _terms(row[1].content)) >= 2
                 or len(query_terms) <= 2 and bool(query_terms & _terms(row[1].content))
                 or row[1].content.casefold() in query.casefold()
                 or query.casefold() in row[1].content.casefold()
             )) or row[1].kind == "preference"),
            key=lambda row: (-row[0], row[1].updated_at, row[1].item_id),
        )
        vector_scores: dict[str, float] = {}
        raw_vector_scores: dict[str, float] = {}
        vector_gate_held = vector_semaphore is not None
        backfill_semaphore = None
        backfill_gate_held = False
        if self.embedding_client is not None and self.embedding_model and ranked:
            try:
                if vector_semaphore is not None and not vector_gate_held:
                    await vector_semaphore.acquire()
                    vector_gate_held = True
                expected_dimensions = getattr(self.embedding_client, "dimensions", None)
                cached = await self.repository.embeddings_for(
                    [(item.item_id, item.version) for _, item, _ in ranked],
                    model_key=self.embedding_model,
                )
                item_vectors = {
                    item_id: vector for item_id, vector in cached.items()
                    if expected_dimensions is None or len(vector) == expected_dimensions
                }
                missing = [row for row in ranked if row[1].item_id not in item_vectors]
                if missing:
                    lexical_ids = {row[1].item_id for row in lexical_order}
                    prioritized = lexical_order + [row for row in ranked if row[1].item_id not in lexical_ids]
                    missing_by_id = {row[1].item_id: row for row in missing}
                    missing = [missing_by_id[row[1].item_id] for row in prioritized
                               if row[1].item_id in missing_by_id][:_ON_DEMAND_EMBED_ITEM_LIMIT]
                # Query embeddings stay independent; only shared fact backfill is coalesced.
                if vector_gate_held:
                    vector_semaphore.release()
                    vector_gate_held = False
                query_vectors = await self.embedding_client.embed([query])
                if len(query_vectors) != 1:
                    raise ValueError("embedding_count_mismatch")
                query_vector = query_vectors[0]
                if missing:
                    backfill_semaphore = getattr(self.repository, "embedding_backfill_semaphore", None)
                    if backfill_semaphore is not None:
                        backfill_semaphore = backfill_semaphore(self.embedding_model)
                        if vector_gate_held:
                            vector_semaphore.release()
                            vector_gate_held = False
                        await backfill_semaphore.acquire()
                        backfill_gate_held = True
                        if vector_semaphore is not None:
                            await vector_semaphore.acquire()
                            vector_gate_held = True
                        cached = await self.repository.embeddings_for(
                            [(item.item_id, item.version) for _, item, _ in ranked],
                            model_key=self.embedding_model,
                        )
                        item_vectors = {
                            item_id: vector for item_id, vector in cached.items()
                            if expected_dimensions is None or len(vector) == expected_dimensions
                        }
                        missing_by_id = {
                            row[1].item_id: row for row in ranked if row[1].item_id not in item_vectors
                        }
                        missing = [missing_by_id[row[1].item_id] for row in prioritized
                                   if row[1].item_id in missing_by_id][:_ON_DEMAND_EMBED_ITEM_LIMIT]
                embed_batch_size = 64
                if vector_gate_held:
                    vector_semaphore.release()
                    vector_gate_held = False
                new_embeddings = []
                for index in range(0, len(missing), embed_batch_size):
                    batch = missing[index:index + embed_batch_size]
                    vectors = await self.embedding_client.embed([
                        f"{item.topic_key}: {item.content}" for _, item, _ in batch
                    ])
                    if len(vectors) != len(batch):
                        raise ValueError("embedding_count_mismatch")
                    for (_, item, _), vector in zip(batch, vectors):
                        if len(vector) != len(query_vector):
                            raise ValueError("embedding_dimension_mismatch")
                        item_vectors[item.item_id] = vector
                        new_embeddings.append((item.item_id, item.version, vector))

                if vector_semaphore is not None:
                    await vector_semaphore.acquire()
                    vector_gate_held = True
                query_norm = math.sqrt(sum(value * value for value in query_vector))
                if query_norm > 0:
                    for _, item, _ in ranked:
                        vector = item_vectors.get(item.item_id)
                        if vector is None or len(vector) != len(query_vector):
                            continue
                        norm = math.sqrt(sum(value * value for value in vector))
                        if norm > 0:
                            cosine = sum(a * b for a, b in zip(query_vector, vector)) / (query_norm * norm)
                            cosine = max(0.0, min(1.0, cosine))
                            raw_vector_scores[item.item_id] = cosine
                            if cosine >= self.min_vector_similarity:
                                vector_scores[item.item_id] = cosine
                    if new_embeddings:
                        try:
                            await self.repository.store_embeddings(
                                model_key=self.embedding_model,
                                dimensions=len(query_vector),
                                embeddings=new_embeddings,
                            )
                        except Exception:
                            pass
            except Exception:
                vector_scores = {}
                raw_vector_scores = {}
            finally:
                if vector_semaphore is not None and not vector_gate_held:
                    await vector_semaphore.acquire()
                    vector_gate_held = True
                if backfill_gate_held:
                    backfill_semaphore.release()

        self.last_vector_scores = raw_vector_scores

        if vector_scores:
            vector_order = sorted(
                (row for row in ranked if vector_scores.get(row[1].item_id, 0.0) > 0),
                key=lambda row: (-vector_scores[row[1].item_id], row[1].item_id),
            )
            lexical_ranks = {item.item_id: rank for rank, (_, item, _) in enumerate(lexical_order, start=1)}
            vector_ranks = {item.item_id: rank for rank, (_, item, _) in enumerate(vector_order, start=1)}
            hybrid_items = [row for row in ranked if row[1].item_id in lexical_ranks or row[1].item_id in vector_ranks]
            hybrid_items.sort(key=lambda row: (
                -((1 / (60 + lexical_ranks[row[1].item_id]) if row[1].item_id in lexical_ranks else 0)
                  + (1 / (60 + vector_ranks[row[1].item_id]) if row[1].item_id in vector_ranks else 0)),
                row[1].item_id,
            ))
            ranked = hybrid_items
        else:
            ranked = lexical_order

        result = []
        used_chars = preference_chars
        for score, item, sources in ranked:
            excerpt = item.content.strip()
            item_chars = len(excerpt) + 64
            if len(result) >= limit or used_chars + item_chars > budget_chars:
                continue
            result.append({
                "item_id": item.item_id,
                "version": item.version,
                "kind": item.kind,
                "scope": item.scope,
                "scope_id": item.scope_id,
                "topic_key": item.topic_key,
                "content": excerpt,
                "score": round(score, 4),
                "vector_similarity": round(vector_scores[item.item_id], 4)
                if item.item_id in vector_scores else None,
                "retrieval_mode": "hybrid" if vector_scores else "keyword",
                "sources": [{
                    "source_system": source["source_system"],
                    "source_installation_id": source["source_installation_id"],
                    "session_id": source["session_id"],
                    "message_id": source["message_id"],
                    "source_revision": source["source_revision"],
                    "role": source["role"],
                } for source in sources],
            })
            used_chars += item_chars
        current_generation = await self.repository.generation()
        if current_generation != generation:
            return {
                "generation": current_generation,
                "core_preferences": [],
                "items": [],
                "used_chars": 0,
                "budget_chars": budget_chars,
                "retrieval_mode": "stale_snapshot_discarded",
            }
        return {
            "generation": current_generation,
            "core_preferences": core_preferences,
            "items": result,
            "used_chars": used_chars,
            "budget_chars": budget_chars,
            "retrieval_mode": "hybrid" if vector_scores else "keyword",
        }
