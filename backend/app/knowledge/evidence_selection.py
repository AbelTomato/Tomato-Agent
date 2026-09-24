from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from app.knowledge.pipeline_models import (
    CandidateEvidence,
    EvidenceSelection,
    QueryPlan,
    SelectionDisposition,
)


class EvidenceSelector(Protocol):
    def select(
        self,
        plan: QueryPlan,
        candidates: Sequence[CandidateEvidence],
        *,
        final_limit: int,
    ) -> EvidenceSelection:
        ...


class BaselineEvidenceSelector:
    """Keep the reranked candidate order and only apply the final limit."""

    def select(
        self,
        plan: QueryPlan,
        candidates: Sequence[CandidateEvidence],
        *,
        final_limit: int,
    ) -> EvidenceSelection:
        if final_limit <= 0:
            raise ValueError("final_limit must be greater than zero")

        selected: list[CandidateEvidence] = []
        seen_chunk_ids: set[str] = set()
        dispositions: list[SelectionDisposition] = []
        selected_order = 0
        for candidate_index, candidate in enumerate(candidates, start=1):
            chunk_id = candidate.result.chunk_id
            if candidate.result.chunk_id in seen_chunk_ids:
                dispositions.append(
                    SelectionDisposition(
                        candidate_index=candidate_index,
                        chunk_id=chunk_id,
                        selected=False,
                        excluded_reason="duplicate_chunk",
                    )
                )
                continue
            if len(selected) >= final_limit:
                dispositions.append(
                    SelectionDisposition(
                        candidate_index=candidate_index,
                        chunk_id=chunk_id,
                        selected=False,
                        excluded_reason="final_limit",
                    )
                )
                seen_chunk_ids.add(chunk_id)
                continue
            selected.append(candidate)
            seen_chunk_ids.add(candidate.result.chunk_id)
            selected_order += 1
            dispositions.append(
                SelectionDisposition(
                    candidate_index=candidate_index,
                    chunk_id=chunk_id,
                    selected=True,
                    selected_order=selected_order,
                )
            )

        covered_query_ids = tuple(
            query.query_id
            for query in plan.queries
            if any(query.query_id in candidate.query_ids for candidate in selected)
        )
        covered_document_ids = tuple(
            dict.fromkeys(candidate.result.document_id for candidate in selected)
        )
        return EvidenceSelection(
            selected=tuple(selected),
            covered_query_ids=covered_query_ids,
            covered_document_ids=covered_document_ids,
            final_limit=final_limit,
            dispositions=tuple(dispositions),
        )


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

        first_candidate_indices: dict[str, int] = {}
        for candidate_index, candidate in enumerate(candidates, start=1):
            first_candidate_indices.setdefault(candidate.result.chunk_id, candidate_index)
        selected_order_by_chunk = {
            candidate.result.chunk_id: index
            for index, candidate in enumerate(selected, start=1)
        }
        dispositions: list[SelectionDisposition] = []
        for candidate_index, candidate in enumerate(candidates, start=1):
            chunk_id = candidate.result.chunk_id
            if first_candidate_indices[chunk_id] != candidate_index:
                disposition = SelectionDisposition(
                    candidate_index=candidate_index,
                    chunk_id=chunk_id,
                    selected=False,
                    excluded_reason="duplicate_chunk",
                )
            elif chunk_id in selected_order_by_chunk:
                disposition = SelectionDisposition(
                    candidate_index=candidate_index,
                    chunk_id=chunk_id,
                    selected=True,
                    selected_order=selected_order_by_chunk[chunk_id],
                )
            elif len(selected) >= final_limit:
                disposition = SelectionDisposition(
                    candidate_index=candidate_index,
                    chunk_id=chunk_id,
                    selected=False,
                    excluded_reason="final_limit",
                )
            else:
                disposition = SelectionDisposition(
                    candidate_index=candidate_index,
                    chunk_id=chunk_id,
                    selected=False,
                    excluded_reason="not_selected_by_selector",
                )
            dispositions.append(disposition)

        return EvidenceSelection(
            selected=tuple(selected),
            covered_query_ids=tuple(query.query_id for query in plan.queries if query.query_id in covered_queries),
            covered_document_ids=tuple(
                document_id
                for document_id in dict.fromkeys(item.result.document_id for item in selected)
            ),
            final_limit=final_limit,
            dispositions=tuple(dispositions),
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