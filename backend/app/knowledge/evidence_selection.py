from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from app.knowledge.pipeline_models import CandidateEvidence, EvidenceSelection, QueryPlan


class EvidenceSelector(Protocol):
    def select(
        self,
        plan: QueryPlan,
        candidates: Sequence[CandidateEvidence],
        *,
        final_limit: int,
    ) -> EvidenceSelection:
        ...


class CoverageAwareEvidenceSelector:
    def select(
        self,
        plan: QueryPlan,
        candidates: Sequence[CandidateEvidence],
        *,
        final_limit: int,
    ) -> EvidenceSelection:
        if final_limit <= 0:
            raise ValueError("final_limit must be greater than zero")

        ordered = self._ordered_unique(candidates)
        selected: list[CandidateEvidence] = []
        covered_queries: set[str] = set()
        covered_documents: set[str] = set()

        for candidate in ordered:
            if len(selected) >= final_limit:
                break
            adds_query = bool(set(candidate.query_ids) - covered_queries)
            adds_document = candidate.result.document_id not in covered_documents
            if adds_query or (adds_document and plan.is_multi_evidence):
                selected.append(candidate)
                covered_queries.update(candidate.query_ids)
                covered_documents.add(candidate.result.document_id)

        for candidate in ordered:
            if len(selected) >= final_limit:
                break
            if candidate.result.chunk_id in {item.result.chunk_id for item in selected}:
                continue
            selected.append(candidate)
            covered_queries.update(candidate.query_ids)
            covered_documents.add(candidate.result.document_id)

        return EvidenceSelection(
            selected=tuple(selected),
            covered_query_ids=tuple(query.query_id for query in plan.queries if query.query_id in covered_queries),
            covered_document_ids=tuple(
                document_id
                for document_id in dict.fromkeys(item.result.document_id for item in selected)
            ),
            final_limit=final_limit,
        )

    def _ordered_unique(
        self, candidates: Sequence[CandidateEvidence]
    ) -> list[CandidateEvidence]:
        unique: dict[str, CandidateEvidence] = {}
        for candidate in candidates:
            unique.setdefault(candidate.result.chunk_id, candidate)
        return sorted(
            unique.values(),
            key=lambda candidate: (
                -(candidate.rerank_score if candidate.rerank_score is not None else float("-inf")),
                min(candidate.retrieval_ranks),
                -candidate.result.score,
                candidate.result.source_path,
                candidate.result.start_line,
                candidate.result.chunk_id,
            ),
        )