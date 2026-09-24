"""Private dev answer evaluation; real provider access is explicitly opt-in."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from app.agent.interfaces import LLMClient
from app.knowledge import evaluation as retrieval_evaluation
from app.knowledge.evaluation import (
    EvaluationQuestion,
    QuestionResult,
    build_knowledge_pipeline,
    evaluate_question_results,
    load_questions,
    summarize_question_results,
)
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.service import (
    GeneratedKnowledgeAnswer,
    KnowledgeService,
)
from app.observability.rag_trace import (
    DataFingerprint,
    HumanAnswerReview,
    TraceRunManifest,
    canonical_json_sha256,
)
from app.observability.rag_trace_sink import (
    RagTraceSink,
    TraceIntegrityError,
    TraceSinkError,
    verify_trace_run,
)
from app.observability.recording_llm_client import (
    LLMObservationError,
    RecordingLLMClient,
    llm_question_scope,
)
from app.llm.compatible_client import OpenAICompatibleClient
from app.settings import settings


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATABASES_ROOT = (PROJECT_ROOT / "backend" / "data" / "rag" / "databases").resolve()
_SAFE_DATASET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_LLM_CALLS = 18


def create_llm_client(args: argparse.Namespace) -> LLMClient | None:
    """Create the real provider client only after explicit CLI opt-in."""
    if not args.real_llm:
        return None
    if not settings.llm_api_key.strip():
        raise ValueError("real LLM mode requires a configured API key")
    if not settings.llm_base_url.strip() or not settings.llm_model.strip():
        raise ValueError("real LLM mode requires a configured base URL and model")
    return OpenAICompatibleClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
    )


def _validate_report_root_ancestors(report_root: Path) -> None:
    """Reject symlink components instead of silently resolving through them."""
    if not report_root.is_absolute():
        raise TraceSinkError("report root must be an absolute path")
    current = Path(report_root.anchor)
    for component in report_root.parts[1:]:
        current /= component
        if current.is_symlink():
            raise TraceSinkError("report root path must not contain symlink ancestors")


@dataclass(frozen=True)
class AnswerEvaluationResult:
    exit_code: int
    run_dir: Path
    manifest: TraceRunManifest
    report: dict[str, Any]


class _RunObserver:
    """Fan observation events into the private sink and retain per-question rows."""

    def __init__(self, sink: RagTraceSink) -> None:
        self.sink = sink
        self.question_id: str | None = None
        self.events: dict[str, list[dict[str, Any]]] = {}

    def begin_question(self, question_id: str) -> None:
        self.question_id = question_id
        self.events.setdefault(question_id, [])

    def end_question(self) -> None:
        self.question_id = None

    def emit(
        self,
        event_type: str,
        *,
        status: str,
        payload: dict[str, Any],
        duration_ms: float | None,
    ) -> None:
        question_id = self.question_id
        if question_id is None:
            raise LLMObservationError("trace event requires an active evaluation question")
        event = self.sink.append_event(
            question_id=question_id,
            event_type=event_type,
            status=status,
            payload=payload,
            duration_ms=duration_ms,
        )
        self.events[question_id].append(event.model_dump(mode="json"))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_database_path(database: Path) -> Path:
    if database.name in {"agent.db", "knowledge.db"}:
        raise ValueError("business databases are not approved; use an independent RAG snapshot")
    try:
        resolved = database.resolve(strict=True)
    except OSError as exc:
        raise ValueError("independent RAG snapshot does not exist") from exc
    if not resolved.is_file() or not _within(resolved, DATABASES_ROOT):
        raise ValueError("database must be an independent RAG snapshot inside backend/data/rag/databases")
    if database.is_symlink():
        raise ValueError("database snapshot must not be a symlink")
    return resolved


def _safe_error_code(error: Exception) -> str:
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, LLMObservationError):
        return "observation_error"
    if isinstance(error, ConnectionError):
        return "client_error"
    if isinstance(error, ValueError):
        return "value_error"
    if isinstance(error, OSError):
        return "io_error"
    return "question_error"


def _events_for(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [event for event in events if event.get("event_type") == event_type]


def _candidate_items(events: list[dict[str, Any]]) -> list[Any]:
    from app.knowledge.pipeline_models import CandidateEvidence

    retrieval_events = _events_for(events, "retrieval.completed")
    if not retrieval_events:
        return []
    merged = retrieval_events[-1]["payload"].get("merged_candidates", [])
    if not isinstance(merged, list):
        return []
    candidates = []
    for candidate in merged:
        if not isinstance(candidate, dict):
            continue
        candidates.append(
            CandidateEvidence.model_validate(
                {
                    key: candidate[key]
                    for key in (
                        "result",
                        "query_ids",
                        "retrieval_ranks",
                        "retrieval_scores",
                        "retrieval_score_type",
                        "rerank_score",
                    )
                    if key in candidate
                }
            )
        )
    return candidates


def _selected_items(events: list[dict[str, Any]]) -> list[Any]:
    from app.knowledge.pipeline_models import CandidateEvidence

    selection_events = _events_for(events, "evidence_selection.completed")
    if not selection_events:
        return []
    selected = selection_events[-1]["payload"].get("selection", {}).get("selected", [])
    return [CandidateEvidence.model_validate(item) for item in selected]


def _phase_metrics(events: list[dict[str, Any]]) -> dict[str, float | None]:
    phase_events = {
        "query_planner": ("query_plan.completed", "query_plan.failed"),
        "candidate": ("retrieval.completed", "retrieval.failed"),
        "rerank": ("rerank.completed", "rerank.failed"),
        "selection": ("evidence_selection.completed", "evidence_selection.failed"),
        "judge": ("answerability.completed", "answerability.failed"),
    }
    result: dict[str, float | None] = {}
    for name, event_types in phase_events.items():
        phase_events_for_question = [
            event
            for event in events
            if event.get("event_type") in event_types and event.get("duration_ms") is not None
        ]
        result[name] = (
            float(phase_events_for_question[-1]["duration_ms"])
            if phase_events_for_question
            else None
        )
    answerer_events = [
        event
        for event in _events_for(events, "llm.response")
        if event.get("payload", {}).get("purpose") == "answerer"
        and event.get("duration_ms") is not None
    ]
    result["llm"] = sum(float(event["duration_ms"]) for event in answerer_events) if answerer_events else None
    return result


def _llm_counts(events: list[dict[str, Any]]) -> dict[str, Any]:
    requests = _events_for(events, "llm.request")
    responses = _events_for(events, "llm.response")
    skipped = _events_for(events, "llm.skipped")
    purposes = ("query_planner", "answerer")
    return {
        "request_count": len(requests),
        "response_count": len(responses),
        "attempt_count": len(requests),
        "failed_count": sum(event.get("status") == "failed" for event in responses),
        "skipped_count": len(skipped),
        "by_purpose": {
            purpose: {
                "request_count": sum(event.get("payload", {}).get("purpose") == purpose for event in requests),
                "failed_count": sum(
                    event.get("payload", {}).get("purpose") == purpose
                    and event.get("status") == "failed"
                    for event in responses
                ),
                "skipped_count": sum(event.get("payload", {}).get("purpose") == purpose for event in skipped),
            }
            for purpose in purposes
        },
    }


def _review_summary(reviews: list[HumanAnswerReview]) -> dict[str, Any]:
    dimensions = ("correctness", "completeness", "groundedness", "citation_fidelity")
    result: dict[str, Any] = {
        "reviewed_count": sum(
            any(getattr(review, dimension) != "not_reviewed" for dimension in dimensions)
            for review in reviews
        ),
        "dimensions": {},
    }
    for dimension in dimensions:
        counts = {status: 0 for status in ("pass", "partial", "fail", "not_reviewed")}
        for review in reviews:
            counts[getattr(review, dimension)] += 1
        sample_count = counts["pass"] + counts["partial"] + counts["fail"]
        result["dimensions"][dimension] = {"sample_count": sample_count, "counts": counts}
    return result


def _question_report(
    question: EvaluationQuestion,
    result: QuestionResult,
    answer: dict[str, Any] | None,
) -> dict[str, Any]:
    item = asdict(result)
    item.update(
        {
            "query": question.query,
            "answerable": question.answerable,
            "reference_answer": question.reference_answer,
        }
    )
    item["answer"] = answer
    return item


def _build_question_trace(
    question: EvaluationQuestion,
    *,
    events: list[dict[str, Any]],
    result: QuestionResult,
    answer: dict[str, Any] | None,
    error_code: str | None,
) -> dict[str, Any]:
    query_plan = next(
        (event["payload"] for event in reversed(events) if event.get("event_type") == "query_plan.completed"),
        None,
    )
    machine_decision = next(
        (event["payload"].get("decision") for event in reversed(events) if event.get("event_type") == "answerability.completed"),
        None,
    )
    citation_validation = next(
        (event["payload"] for event in reversed(events) if event.get("event_type") == "citation_validation.completed"),
        None,
    )
    return {
        "split": "dev",
        "query": question.query,
        "category": question.category,
        "answerable": question.answerable,
        "reference_answer": question.reference_answer,
        "relevant_spans": list(question.relevant_spans),
        "execution_status": "error" if error_code else result.status,
        "error": {"code": error_code} if error_code else None,
        "query_plan": query_plan,
        "machine_decision": machine_decision,
        "citation_validation": citation_validation,
        "answer": answer,
        "machine_answer_quality": "not_evaluated",
        "retrieval_metrics": {
            "candidate_evidence_recall_at_k": result.candidate_evidence_recall_at_k,
            "candidate_mrr_at_k": result.candidate_mrr_at_k,
            "candidate_hit_at_k": result.candidate_hit_at_k,
            "final_evidence_recall": result.final_evidence_recall,
            "source_coverage": result.source_coverage,
        },
        "phase_latency_ms": result.phase_latency_ms,
        "end_to_end_latency_ms": result.latency_ms,
        "llm_called": result.llm_called,
        "llm_interface_boundary": "Python LLMClient.complete(messages, tools); not provider HTTP payload",
        "events": events,
    }


async def run_answer_evaluation(
    args: argparse.Namespace,
    *,
    llm_client: LLMClient | None = None,
    run_id: UUID | None = None,
) -> AnswerEvaluationResult:
    """Run answer generation and audit on only the selected dev questions."""
    if args.split != "dev":
        raise ValueError("answer evaluation accepts only the dev split")
    if args.mode != "keyword":
        raise ValueError("answer evaluation currently accepts keyword mode only")
    if args.candidate_limit <= 0 or args.final_limit <= 0:
        raise ValueError("candidate_limit and final_limit must be positive")
    if args.candidate_limit < args.final_limit:
        raise ValueError("candidate_limit must not be smaller than final_limit")
    if args.query_planning not in {"disabled", "conditional"}:
        raise ValueError("unsupported query planning strategy")
    if args.real_llm and (args.query_planning != "disabled" or settings.knowledge_query_planning_enabled):
        raise ValueError("real LLM mode requires query planning to be disabled")
    if args.real_llm and settings.knowledge_answerability_allow_insufficient_llm:
        raise ValueError("real LLM mode requires insufficient-evidence LLM calls to be disabled")
    if args.rerank not in {"noop", "offline-fake"}:
        raise ValueError("answer evaluation permits only noop or offline-fake reranking")
    if args.evidence_selection not in {"baseline", "coverage-aware"}:
        raise ValueError("unsupported evidence selection strategy")
    if args.answerability not in {"baseline", "coverage-v1"}:
        raise ValueError("unsupported answerability strategy")

    dataset_input_path = Path(args.dataset)
    if dataset_input_path.is_symlink():
        raise ValueError("evaluation dataset must not be a symlink")
    dataset_path = dataset_input_path.resolve(strict=True)
    if not dataset_path.is_file():
        raise ValueError("evaluation dataset must be a regular file")
    dataset_sha256_before = _file_sha256(dataset_path)
    database_path = _validate_database_path(Path(args.database))
    report_root = Path(args.report_root)
    _validate_report_root_ancestors(report_root)
    dataset_name = args.dataset_name
    if not isinstance(dataset_name, str) or not _SAFE_DATASET_NAME.fullmatch(dataset_name):
        raise ValueError("dataset_name must be a safe single path component")

    # load_questions validates only the selected dev records: other split rows
    # are discarded immediately after parsing their JSON object and split tag.
    questions = load_questions(dataset_path, split="dev")
    if not questions:
        raise ValueError("dataset contains no dev questions")
    if any(question.split != "dev" for question in questions):
        raise ValueError("dataset contains a non-dev question in the selected split")

    started_at = datetime.now(timezone.utc)
    initial_snapshot_sha256 = _file_sha256(database_path)
    repository = KnowledgeRepository(database_path, read_only=True)
    await repository.init()
    documents = await repository.list_documents()
    document_manifest_sha256 = canonical_json_sha256(
        [
            {
                "document_id": document.document_id,
                "source_path": document.source_path,
                "document_version": document.document_version,
            }
            for document in sorted(documents, key=lambda item: item.document_id)
        ]
    )
    dataset_fingerprint = DataFingerprint(
        path_identifier=dataset_path.name,
        sha256=dataset_sha256_before,
        version=f"sha256:{dataset_sha256_before}",
        manifest_sha256=dataset_sha256_before,
    )
    snapshot_fingerprint = DataFingerprint(
        path_identifier=database_path.relative_to(DATABASES_ROOT).as_posix(),
        sha256=initial_snapshot_sha256,
        version=f"sha256:{initial_snapshot_sha256}",
        manifest_sha256=document_manifest_sha256,
    )
    selected_run_id = run_id or uuid4()
    config = {
        "mode": args.mode,
        "candidate_limit": args.candidate_limit,
        "final_limit": args.final_limit,
        "query_planning": args.query_planning,
        "rerank": args.rerank,
        "evidence_selection": args.evidence_selection,
        "answerability": args.answerability,
        "answerability_allow_insufficient_llm": settings.knowledge_answerability_allow_insufficient_llm,
        "llm_client": (
            "real_provider" if args.real_llm
            else "injected" if llm_client is not None
            else "not_configured"
        ),
        "llm_model_id": args.llm_model_id if llm_client is not None else None,
        "llm_max_requests": MAX_LLM_CALLS if llm_client is not None else None,
        "llm_mode": "real_opt_in" if args.real_llm else "offline_or_injected",
    }
    sink = RagTraceSink(
        report_root=report_root,
        dataset_name=dataset_name,
        run_id=selected_run_id,
        expected_question_ids=[question.question_id for question in questions],
        started_at=started_at,
        dataset=dataset_fingerprint,
        snapshot=snapshot_fingerprint,
        implementation={
            "runner": "answer-evaluation/v1",
            "runner_sha256": _file_sha256(Path(__file__)),
            "retrieval_evaluation_sha256": _file_sha256(Path(retrieval_evaluation.__file__)),
        },
        configuration=config,
    )
    observer = _RunObserver(sink)
    pipeline = build_knowledge_pipeline(args, repository, observer=observer)
    service = KnowledgeService(
        repository,
        pipeline=pipeline,
        candidate_limit=args.candidate_limit,
        allow_insufficient_llm=settings.knowledge_answerability_allow_insufficient_llm,
    )
    recorded_client: RecordingLLMClient | None = None
    if llm_client is not None:
        recorded_client = RecordingLLMClient(
            llm_client,
            observer=observer,
            run_id=selected_run_id,
            model_id=args.llm_model_id,
            max_calls=MAX_LLM_CALLS,
        )

    question_results: list[QuestionResult] = []
    answer_results: dict[str, dict[str, Any] | None] = {}
    execution_failures = 0

    for question in questions:
        question_id = question.question_id
        observer.begin_question(question_id)
        observer.emit(
            "question.started",
            status="success",
            payload={
                "split": "dev",
                "query_sha256": hashlib.sha256(question.query.encode("utf-8")).hexdigest(),
            },
            duration_ms=None,
        )
        started = time.perf_counter()
        answer_payload: dict[str, Any] | None = None
        error_code: str | None = None
        try:
            with llm_question_scope(question_id):
                answer = await service.answer(
                    question.query,
                    mode=args.mode,
                    limit=args.final_limit,
                    llm_client=recorded_client,
                )
            generated = isinstance(answer, GeneratedKnowledgeAnswer)
            answer_events = _events_for(observer.events[question_id], "answer.completed")
            observed_answer = answer_events[-1]["payload"] if answer_events else {}
            answer_payload = {
                "answer": answer.answer,
                "citation_ids": observed_answer.get("citation_ids", []) if generated else [],
                "evidence_status": answer.evidence_status,
                "generated": generated,
            }
        except Exception as exc:
            error_code = _safe_error_code(exc)
            execution_failures += 1
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000

        events = observer.events[question_id]
        if error_code is not None:
            # A stage may fail before producing an observation; still leave an
            # explicit, credential-free event for the attempted question.
            observer.emit(
                "question.execution",
                status="failed",
                payload={"error_code": error_code},
                duration_ms=elapsed_ms,
            )
            events = observer.events[question_id]

        candidates = _candidate_items(events)
        selected = _selected_items(events)
        phase_latency = _phase_metrics(events)
        request_events = _events_for(events, "llm.request")
        citation_events = _events_for(events, "citation_validation.completed")
        plan_events = _events_for(events, "query_plan.completed")
        decision_events = _events_for(events, "answerability.completed")
        citation_complete = (
            bool(citation_events[-1]["payload"].get("validation_passed"))
            if citation_events
            else False if answer_payload is not None and answer_payload["generated"] else None
        )
        query_plan = plan_events[-1]["payload"] if plan_events else None
        if decision_events:
            decision = decision_events[-1]["payload"].get("decision", {})
            evidence_status = decision.get("status")
            judge_reason = decision.get("reason")
        else:
            evidence_status = None
            judge_reason = None

        metrics = evaluate_question_results(
            [question],
            {question_id: selected},
            candidate_result_map={question_id: candidates},
            final_result_map={question_id: selected},
            error_map={question_id: error_code} if error_code else {},
            score_type="bm25",
            candidate_score_type="bm25",
            final_score_type="bm25",
            latency_map={question_id: elapsed_ms},
            k=args.final_limit,
            candidate_limit=args.candidate_limit,
            phase_latency_map={question_id: phase_latency},
            llm_called_map={question_id: bool(request_events)},
            citation_complete_map={question_id: citation_complete},
            query_plan_map={question_id: query_plan} if plan_events else {},
            judge_reason_map={question_id: judge_reason} if decision_events else {},
            judge_executed_map={question_id: bool(decision_events)},
            evidence_status_map={question_id: evidence_status} if decision_events else {},
        )[0]
        question_results.append(metrics)
        answer_results[question_id] = answer_payload
        sink.write_question(
            question_id,
            _build_question_trace(
                question,
                events=events,
                result=metrics,
                answer=answer_payload,
                error_code=error_code,
            ),
        )
        observer.end_question()

    try:
        final_snapshot_sha256 = _file_sha256(database_path)
        snapshot_unchanged = final_snapshot_sha256 == initial_snapshot_sha256
    except OSError:
        final_snapshot_sha256 = None
        snapshot_unchanged = False
    try:
        dataset_sha256_after = _file_sha256(dataset_path)
        dataset_unchanged = dataset_sha256_after == dataset_sha256_before
    except OSError:
        dataset_sha256_after = None
        dataset_unchanged = False

    pending_reviews = [
        HumanAnswerReview(
            question_id=question.question_id,
            reviewer_id=None,
            reviewed_at=None,
            correctness="not_reviewed",
            completeness="not_reviewed",
            groundedness="not_reviewed",
            citation_fidelity="not_reviewed",
            citation_chunk_ids=(),
            rationale=None,
        )
        for question in questions
    ]
    retrieval_summary = summarize_question_results(question_results, k=args.final_limit)
    report: dict[str, Any] = {
        "split": "dev",
        "run_id": str(selected_run_id),
        "trace_schema": "rag-observe-trace/v1",
        "status": (
            "incomplete"
            if execution_failures or not snapshot_unchanged or not dataset_unchanged
            else "complete"
        ),
        "question_count": len(questions),
        "execution_error_count": execution_failures,
        "snapshot_unchanged": snapshot_unchanged,
        "snapshot_sha256_before": initial_snapshot_sha256,
        "snapshot_sha256_after": final_snapshot_sha256,
        "dataset_unchanged": dataset_unchanged,
        "dataset_sha256_before": dataset_sha256_before,
        "dataset_sha256_after": dataset_sha256_after,
        "retrieval_summary": asdict(retrieval_summary),
        "answer_quality": {"status": "not_evaluated", "sample_count": 0},
        "human_review": _review_summary(pending_reviews),
        "llm_execution": _llm_counts(
            [event for question in questions for event in observer.events[question.question_id]]
        ),
        "provider_usage_and_cost": "unknown; client does not expose provider usage or billing",
        "provenance": {
            "dataset_sha256": dataset_fingerprint.sha256,
            "snapshot_path_identifier": snapshot_fingerprint.path_identifier,
            "snapshot_sha256": snapshot_fingerprint.sha256,
            "document_manifest_sha256": snapshot_fingerprint.manifest_sha256,
            "configuration": config,
        },
        "question_results": [
            _question_report(
                next(question for question in questions if question.question_id == result.question_id),
                result,
                answer_results[result.question_id],
            )
            for result in question_results
        ],
    }
    by_category = summarize_question_results(question_results, k=args.final_limit, group_by="category")
    report["by_category"] = {name: asdict(summary) for name, summary in by_category.items()}
    by_answerability = summarize_question_results(question_results, k=args.final_limit, group_by="answerable")
    report["by_answerability"] = {name: asdict(summary) for name, summary in by_answerability.items()}
    sink.write_artifact("summary.json", json.dumps(report, ensure_ascii=False, separators=(",", ":")))

    if execution_failures:
        manifest = sink.mark_incomplete("question_execution_failed")
    elif not snapshot_unchanged:
        manifest = sink.mark_incomplete("snapshot_changed")
    elif not dataset_unchanged:
        manifest = sink.mark_incomplete("dataset_changed")
    else:
        manifest = sink.complete()

    try:
        manifest = verify_trace_run(sink.run_dir)
    except TraceIntegrityError:
        manifest = sink.mark_incomplete("artifact_integrity_failed")
        report["artifact_integrity"] = "failed"
        # The summary itself is already included in the run's artifact list.
        # Its payload records the primary run facts; integrity failure is
        # independently classified in failures.json and the final manifest.
        return AnswerEvaluationResult(1, sink.run_dir, manifest, report)

    report["artifact_integrity"] = "passed"
    return AnswerEvaluationResult(
        0 if manifest.status == "complete" else 1,
        sink.run_dir,
        manifest,
        report,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run private RAG answer evaluation (dev only)")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--report-root", required=True)
    parser.add_argument("--dataset-name", default="public-blog")
    parser.add_argument("--split", choices=("dev",), default="dev")
    parser.add_argument("--mode", choices=("keyword",), default="keyword")
    parser.add_argument("--candidate-limit", type=int, default=30)
    parser.add_argument("--final-limit", type=int, default=5)
    parser.add_argument("--embedding-model", default=settings.embedding_model)
    parser.add_argument("--embedding-dimensions", type=int, default=settings.embedding_dimensions)
    parser.add_argument(
        "--candidate-min-vector-similarity",
        type=float,
        default=settings.knowledge_candidate_min_vector_similarity,
    )
    parser.add_argument("--query-planning", choices=("disabled", "conditional"), default="disabled")
    parser.add_argument("--rerank", choices=("noop", "offline-fake"), default="noop")
    parser.add_argument("--evidence-selection", choices=("baseline", "coverage-aware"), default="baseline")
    parser.add_argument("--answerability", choices=("baseline", "coverage-v1"), default="baseline")
    parser.add_argument("--llm-model-id", default="injected-offline-client")
    parser.add_argument("--run-id", type=UUID, help="optional preallocated unique run UUID")
    parser.add_argument(
        "--real-llm",
        action="store_true",
        help="explicitly allow real provider requests (maximum 18 client calls)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        llm_client = create_llm_client(args)
        if args.real_llm:
            args.llm_model_id = settings.llm_model
        result = asyncio.run(
            run_answer_evaluation(args, llm_client=llm_client, run_id=args.run_id)
        )
    except (OSError, ValueError, TraceSinkError) as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps({"run_dir": str(result.run_dir), "status": result.manifest.status}, ensure_ascii=False))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())