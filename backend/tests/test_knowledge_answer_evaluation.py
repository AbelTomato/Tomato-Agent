from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path
from uuid import UUID

import pytest

from app.agent.models import LLMResponse, Message, ToolDefinition
from app.knowledge.ingestion import ingest_manifest
from app.knowledge.repository import KnowledgeRepository
from app.observability.rag_trace import TRACE_SCHEMA, canonical_json_sha256
from app.observability.rag_trace_sink import (
    DEFAULT_REPORTS_ROOT,
    RagTraceSink,
    TraceIntegrityError,
    TraceSinkError,
    verify_trace_run,
)
from app.observability.recording_llm_client import RecordingLLMClient


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATABASES_ROOT = (PROJECT_ROOT / "backend" / "data" / "rag" / "databases").resolve()
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "knowledge"
RUN_ID = UUID("e30cc438-76f8-419b-8c37-06c42f24ad51")


class FakeLLM:
    def __init__(self, responses: list[LLMResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[list[Message], list[ToolDefinition]]] = []

    async def complete(self, messages, tools):
        self.calls.append((messages, tools))
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, Exception):
            raise response
        return response


def answer_response(answer: str = "SETEX 会同时设置键的过期时间。") -> LLMResponse:
    return LLMResponse(
        kind="final",
        content=json.dumps(
            {
                "answer": answer,
                "citation_ids": ["R1"],
                "evidence_status": "supported",
            },
            ensure_ascii=False,
        ),
    )


def create_inputs(
    directory: Path,
    *,
    questions: list[tuple[str, str]] | None = None,
    include_test_record: bool = True,
) -> tuple[Path, Path, list[str]]:
    database = directory / "fixed-offline-snapshot.db"
    repository = KnowledgeRepository(database)
    asyncio.run(repository.init())
    asyncio.run(ingest_manifest(repository, FIXTURE_ROOT / "manifest.json", FIXTURE_ROOT))
    redis_document = asyncio.run(repository.get_document_by_path("redis.md"))
    assert redis_document is not None
    questions = questions or [("dev-001", "SETEX 过期时间")]
    records = [
        {
            "id": question_id,
            "query": query,
            "category": "代码/API",
            "split": "dev",
            "answerable": True,
            "relevant_spans": [
                {
                    "document_id": redis_document.document_id,
                    "document_version": redis_document.document_version,
                    "start_line": 5,
                    "end_line": 7,
                }
            ],
            "reference_answer": "设置键的过期时间",
        }
        for question_id, query in questions
    ]
    if include_test_record:
        # The test payload is deliberately invalid apart from its split. The
        # dev runner must not validate, report, or execute it.
        records.append(
            {
                "split": "test",
                "query": "DO_NOT_RECORD_TEST_CONTENT",
                "invalid_test_payload": {"citation_token": "not-for-dev"},
            }
        )
    dataset = directory / "questions.jsonl"
    dataset.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    return dataset, database, [question_id for question_id, _ in questions]


def make_args(dataset: Path, database: Path, report_root: Path, *extra: str):
    from app.knowledge.answer_evaluation import build_parser

    return build_parser().parse_args(
        [
            "--dataset",
            str(dataset),
            "--database",
            str(database),
            "--report-root",
            str(report_root),
            "--split",
            "dev",
            "--mode",
            "keyword",
            "--candidate-limit",
            "10",
            "--final-limit",
            "3",
            *extra,
        ]
    )


def private_reports_root() -> tempfile.TemporaryDirectory[str]:
    DEFAULT_REPORTS_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=".test-answer-eval-", dir=DEFAULT_REPORTS_ROOT)


def private_database_root() -> tempfile.TemporaryDirectory[str]:
    DATABASES_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=".test-answer-eval-", dir=DATABASES_ROOT)


def test_answer_runner_records_answer_context_metrics_and_private_artifacts():
    from app.knowledge.answer_evaluation import run_answer_evaluation

    with private_database_root() as database_directory, private_reports_root() as report_directory:
        dataset, database, question_ids = create_inputs(Path(database_directory))
        original_snapshot_hash = hashlib.sha256(database.read_bytes()).hexdigest()
        fake = FakeLLM([answer_response()])
        args = make_args(dataset, database, Path(report_directory))

        result = asyncio.run(
            run_answer_evaluation(args, llm_client=fake, run_id=RUN_ID)
        )

        assert result.exit_code == 0
        assert result.manifest.trace_schema == TRACE_SCHEMA
        assert result.manifest.status == "complete"
        assert result.manifest.split == "dev"
        assert result.manifest.snapshot.sha256 == original_snapshot_hash
        assert hashlib.sha256(database.read_bytes()).hexdigest() == original_snapshot_hash
        assert result.report["question_count"] == 1
        assert result.report["answer_quality"]["status"] == "not_evaluated"
        assert result.report["human_review"]["reviewed_count"] == 0
        assert result.report["question_results"][0]["question_id"] == question_ids[0]
        assert result.report["question_results"][0]["answer"]["answer"] == "SETEX 会同时设置键的过期时间。"
        assert result.report["question_results"][0]["answer"]["citation_ids"] == ["R1"]
        assert result.report["provider_usage_and_cost"] == (
            "unknown; client does not expose provider usage or billing"
        )
        assert fake.calls and fake.calls[0][1] == []
        assert "SETEX 过期时间" in fake.calls[0][0][1].content

        verified = verify_trace_run(result.run_dir)
        assert verified.status == "complete"
        question_trace = json.loads(
            (result.run_dir / "questions" / "dev-001.json").read_text(encoding="utf-8")
        )
        assert question_trace["trace"]["query"] == "SETEX 过期时间"
        assert question_trace["trace"]["machine_answer_quality"] == "not_evaluated"
        assert (result.run_dir / "human-reviews.jsonl").is_file()
        summary = json.loads((result.run_dir / "summary.json").read_text(encoding="utf-8"))
        assert summary["split"] == "dev"
        assert "summary.json" in {artifact.relative_path for artifact in verified.artifacts}
        assert summary["human_review"]["dimensions"]["groundedness"]["sample_count"] == 0
        assert summary["question_count"] == len(question_ids) == 1
        assert len(summary["question_results"]) == len(question_ids)
        assert len(list((result.run_dir / "questions").glob("*.json"))) == len(question_ids)

        manifest_json = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
        metadata = json.loads((result.run_dir / "run-metadata.json").read_text(encoding="utf-8"))
        assert manifest_json["run_id"] == str(RUN_ID)
        assert manifest_json["trace_schema"] == TRACE_SCHEMA
        assert manifest_json["split"] == "dev"
        assert manifest_json["status"] == "complete"
        assert metadata["expected_question_ids"] == question_ids

        event_lines = (result.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        events = [json.loads(line) for line in event_lines]
        assert events
        expected_sequence = 1
        previous_event_sha256 = None
        for event in events:
            assert event["sequence"] == expected_sequence
            assert event["previous_event_sha256"] == previous_event_sha256
            event_content = {key: value for key, value in event.items() if key != "event_sha256"}
            assert event["event_sha256"] == canonical_json_sha256(event_content)
            previous_event_sha256 = event["event_sha256"]
            expected_sequence += 1

        question_events = [event for event in events if event["question_id"] == question_ids[0]]
        assert question_trace["trace"]["events"] == question_events
        assert len(question_events) == len(question_trace["trace"]["events"])
        assert {
            "question.started",
            "query_plan.completed",
            "retrieval.completed",
            "rerank.completed",
            "evidence_selection.completed",
            "answerability.completed",
            "llm.request",
            "llm.response",
            "citation_validation.completed",
            "answer.completed",
        }.issubset({event["event_type"] for event in question_events})
        question_started = question_events[0]
        assert question_started["event_type"] == "question.started"
        assert question_started["status"] == "success"
        assert question_started["payload"] == {
            "split": "dev",
            "query_sha256": hashlib.sha256("SETEX 过期时间".encode("utf-8")).hexdigest(),
        }
        assert all(
            event["status"] in {"success", "skipped"}
            for event in question_events
            if event["event_type"] in {
                "query_plan.completed",
                "retrieval.completed",
                "rerank.completed",
                "evidence_selection.completed",
                "answerability.completed",
                "citation_validation.completed",
            }
        )
        answer_trace_event = next(
            event for event in question_events if event["event_type"] == "citation_validation.completed"
        )
        assert answer_trace_event["payload"]["whitelist_valid"] is True
        assert answer_trace_event["payload"]["citation_ids"] == result.report["question_results"][0]["answer"]["citation_ids"]
        assert answer_trace_event["payload"]["mapped_citations"]

        phase_latency = question_trace["trace"]["phase_latency_ms"]
        assert question_trace["trace"]["end_to_end_latency_ms"] > 0
        assert phase_latency["candidate"] >= 0
        assert phase_latency["llm"] >= 0
        for event in question_events:
            if event["event_type"].endswith(".completed") and event["duration_ms"] is not None:
                assert event["duration_ms"] >= 0

        artifact_digests = {artifact["relative_path"]: artifact for artifact in manifest_json["artifacts"]}
        for relative_path, artifact in artifact_digests.items():
            artifact_bytes = (result.run_dir / relative_path).read_bytes()
            assert artifact["size_bytes"] == len(artifact_bytes)
            assert artifact["sha256"] == hashlib.sha256(artifact_bytes).hexdigest()
        assert "manifest.json" not in artifact_digests

        all_artifact_text = "".join(
            path.read_text(encoding="utf-8")
            for path in result.run_dir.rglob("*")
            if path.is_file()
        )
        assert "DO_NOT_RECORD_TEST_CONTENT" not in all_artifact_text
        assert "invalid_test_payload" not in all_artifact_text


def test_answer_runner_records_non_llm_path_as_not_called_for_no_results():
    from app.knowledge.answer_evaluation import run_answer_evaluation

    with private_database_root() as database_directory, private_reports_root() as report_directory:
        dataset, database, _ = create_inputs(
            Path(database_directory), questions=[("dev-empty", "xylophone-unmatched-token")]
        )
        args = make_args(dataset, database, Path(report_directory))

        result = asyncio.run(run_answer_evaluation(args, llm_client=None, run_id=RUN_ID))

        assert result.exit_code == 0
        events = [
            json.loads(line)
            for line in (result.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert not any(
            event["event_type"] in {"llm.request", "llm.response"}
            for event in events
        )
        assert any(
            event["event_type"] == "llm.skipped"
            and event["payload"]["purpose"] == "answerer"
            for event in events
        )
        assert result.report["llm_execution"]["request_count"] == 0


def test_answer_runner_real_llm_is_opt_in_and_client_factory_is_offline_by_default(monkeypatch):
    from app.knowledge.answer_evaluation import build_parser, create_llm_client

    default_args = build_parser().parse_args(
        ["--dataset", "questions.jsonl", "--database", "snapshot.db", "--report-root", "."]
    )
    assert default_args.real_llm is False
    assert create_llm_client(default_args) is None

    real_args = build_parser().parse_args(
        [
            "--dataset", "questions.jsonl", "--database", "snapshot.db", "--report-root", ".",
            "--real-llm",
        ]
    )
    monkeypatch.setattr("app.knowledge.answer_evaluation.settings.llm_api_key", "trusted-test-key")
    client = create_llm_client(real_args)
    assert client is not None
    assert client.api_key == "trusted-test-key"
    assert client.model == "gpt-5.6-terra"


def test_answer_runner_real_llm_requires_configured_credentials(monkeypatch):
    from app.knowledge.answer_evaluation import build_parser, create_llm_client

    args = build_parser().parse_args(
        [
            "--dataset", "questions.jsonl", "--database", "snapshot.db", "--report-root", ".",
            "--real-llm",
        ]
    )
    monkeypatch.setattr("app.knowledge.answer_evaluation.settings.llm_api_key", " ")

    with pytest.raises(ValueError, match="configured API key"):
        create_llm_client(args)


def test_answer_runner_caps_real_llm_calls_at_eighteen():
    from app.knowledge.answer_evaluation import run_answer_evaluation

    with private_database_root() as database_directory, private_reports_root() as report_directory:
        questions = [(f"dev-{index:03d}", "SETEX 过期时间") for index in range(1, 20)]
        dataset, database, _ = create_inputs(Path(database_directory), questions=questions)
        fake = FakeLLM([answer_response() for _ in range(18)])
        args = make_args(dataset, database, Path(report_directory))

        result = asyncio.run(run_answer_evaluation(args, llm_client=fake, run_id=RUN_ID))

        assert len(fake.calls) == 18
        assert result.exit_code == 1
        assert result.report["question_count"] == 19
        assert result.report["execution_error_count"] == 1
        assert result.report["llm_execution"]["request_count"] == 18
        assert result.report["llm_execution"]["attempt_count"] == 18


def test_answer_runner_rejects_real_llm_with_query_planning_before_request():
    from app.knowledge.answer_evaluation import run_answer_evaluation

    with private_database_root() as database_directory, private_reports_root() as report_directory:
        dataset, database, _ = create_inputs(Path(database_directory))
        fake = FakeLLM([answer_response()])
        args = make_args(dataset, database, Path(report_directory), "--real-llm", "--query-planning", "conditional")

        with pytest.raises(ValueError, match="query planning to be disabled"):
            asyncio.run(run_answer_evaluation(args, llm_client=fake, run_id=RUN_ID))

        assert fake.calls == []


def test_answer_runner_rejects_symlinked_report_root_ancestor_before_creating_run():
    from app.knowledge.answer_evaluation import run_answer_evaluation

    with private_database_root() as database_directory, private_reports_root() as report_directory:
        dataset, database, _ = create_inputs(Path(database_directory))
        alias = DEFAULT_REPORTS_ROOT / f".test-answer-eval-link-{RUN_ID}"
        alias.symlink_to(Path(report_directory), target_is_directory=True)
        fake = FakeLLM([answer_response()])
        args = make_args(dataset, database, alias)

        try:
            with pytest.raises(TraceSinkError, match="symlink"):
                asyncio.run(run_answer_evaluation(args, llm_client=fake, run_id=RUN_ID))
            assert fake.calls == []
            assert not (Path(report_directory) / "2026-09-24").exists()
        finally:
            alias.unlink()


def test_answer_runner_marks_provider_failure_incomplete_without_losing_trace():
    from app.knowledge.answer_evaluation import run_answer_evaluation

    with private_database_root() as database_directory, private_reports_root() as report_directory:
        dataset, database, _ = create_inputs(Path(database_directory))
        fake = FakeLLM([ConnectionError("private provider diagnostic must not leak")])
        args = make_args(dataset, database, Path(report_directory))

        result = asyncio.run(
            run_answer_evaluation(args, llm_client=fake, run_id=RUN_ID)
        )

        assert result.exit_code == 1
        assert result.manifest.status == "incomplete"
        assert result.report["execution_error_count"] == 1
        assert (result.run_dir / "questions" / "dev-001.json").is_file()
        assert verify_trace_run(result.run_dir).status == "incomplete"
        event_text = (result.run_dir / "events.jsonl").read_text(encoding="utf-8")
        assert "client_error" in event_text
        assert "private provider diagnostic" not in event_text


def test_answer_runner_file_write_failure_keeps_run_incomplete(monkeypatch):
    from app.knowledge.answer_evaluation import run_answer_evaluation

    with private_database_root() as database_directory, private_reports_root() as report_directory:
        dataset, database, _ = create_inputs(Path(database_directory))
        fake = FakeLLM([answer_response()])
        args = make_args(dataset, database, Path(report_directory))
        original_atomic_write = RagTraceSink._atomic_write
        failed_once = False

        def fail_question_write(path: Path, content: bytes) -> None:
            nonlocal failed_once
            if path.parent.name == "questions" and not failed_once:
                failed_once = True
                raise OSError("private filesystem diagnostic")
            original_atomic_write(path, content)

        monkeypatch.setattr(RagTraceSink, "_atomic_write", staticmethod(fail_question_write))
        with pytest.raises(OSError, match="private filesystem diagnostic"):
            asyncio.run(run_answer_evaluation(args, llm_client=fake, run_id=RUN_ID))

        run_dir = next(Path(report_directory).rglob(str(RUN_ID)))
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        failures = json.loads((run_dir / "failures.json").read_text(encoding="utf-8"))
        assert manifest["status"] == "incomplete"
        assert failures == [{"code": "artifact_write_failed"}]
        assert not (run_dir / "questions" / "dev-001.json").exists()
        assert verify_trace_run(run_dir).status == "incomplete"
        assert "private filesystem diagnostic" not in (run_dir / "failures.json").read_text(encoding="utf-8")


def test_answer_runner_continues_after_one_question_fails_and_preserves_both_traces():
    from app.knowledge.answer_evaluation import run_answer_evaluation

    with private_database_root() as database_directory, private_reports_root() as report_directory:
        dataset, database, question_ids = create_inputs(
            Path(database_directory),
            # Both questions must reach the answerer so this case verifies
            # that the second question runs after the first LLM call fails.
            questions=[("dev-001", "SETEX 过期时间"), ("dev-002", "SETEX 过期时间")],
        )
        fake = FakeLLM([TimeoutError("private timeout detail"), answer_response("后续题仍继续")])
        args = make_args(dataset, database, Path(report_directory))

        result = asyncio.run(
            run_answer_evaluation(args, llm_client=fake, run_id=RUN_ID)
        )

        assert result.exit_code == 1
        assert result.manifest.status == "incomplete"
        assert len(fake.calls) == 2
        assert result.report["execution_error_count"] == 1
        assert [item["question_id"] for item in result.report["question_results"]] == question_ids
        assert all(
            (result.run_dir / "questions" / f"{question_id}.json").is_file()
            for question_id in question_ids
        )
        assert verify_trace_run(result.run_dir).status == "incomplete"


def test_answer_runner_rejects_existing_run_directory_without_overwriting():
    from app.knowledge.answer_evaluation import run_answer_evaluation

    with private_database_root() as database_directory, private_reports_root() as report_directory:
        dataset, database, _ = create_inputs(Path(database_directory))
        args = make_args(dataset, database, Path(report_directory))
        first = asyncio.run(run_answer_evaluation(args, llm_client=None, run_id=RUN_ID))
        before = (first.run_dir / "manifest.json").read_bytes()

        with pytest.raises(TraceSinkError, match="already exists"):
            asyncio.run(run_answer_evaluation(args, llm_client=None, run_id=RUN_ID))

        assert (first.run_dir / "manifest.json").read_bytes() == before


def test_answer_runner_marks_artifact_hash_verification_failure_incomplete(monkeypatch):
    import app.knowledge.answer_evaluation as answer_evaluation

    with private_database_root() as database_directory, private_reports_root() as report_directory:
        dataset, database, _ = create_inputs(Path(database_directory))
        args = make_args(dataset, database, Path(report_directory))

        def fail_verification(_run_dir):
            raise TraceIntegrityError("forced artifact hash mismatch")

        monkeypatch.setattr(answer_evaluation, "verify_trace_run", fail_verification)
        result = asyncio.run(
            answer_evaluation.run_answer_evaluation(args, llm_client=None, run_id=RUN_ID)
        )

        assert result.exit_code == 1
        assert result.manifest.status == "incomplete"
        assert verify_trace_run(result.run_dir).status == "incomplete"
        failures = json.loads((result.run_dir / "failures.json").read_text(encoding="utf-8"))
        assert failures == [{"code": "artifact_integrity_failed"}]


def test_answer_runner_rejects_non_dev_split_and_unapproved_database_path():
    from app.knowledge.answer_evaluation import build_parser, run_answer_evaluation

    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["--dataset", "questions.jsonl", "--database", "snapshot.db", "--report-root", ".", "--split", "test"]
        )

    with private_database_root() as database_directory, private_reports_root() as report_directory:
        dataset, database, _ = create_inputs(Path(database_directory))
        args = make_args(dataset, database, Path(report_directory))
        args.database = str(PROJECT_ROOT / "backend" / "data" / "knowledge.db")

        with pytest.raises(ValueError, match="independent RAG snapshot"):
            asyncio.run(run_answer_evaluation(args, llm_client=None, run_id=RUN_ID))


def test_answer_runner_rejects_report_directory_outside_approved_reports_root():
    from app.knowledge.answer_evaluation import run_answer_evaluation

    with private_database_root() as database_directory:
        dataset, database, _ = create_inputs(Path(database_directory))
        args = make_args(dataset, database, PROJECT_ROOT / "backend" / "data")

        with pytest.raises(TraceSinkError, match="inside backend/data/rag/reports"):
            asyncio.run(run_answer_evaluation(args, llm_client=None, run_id=RUN_ID))


def test_answer_runner_detects_snapshot_hash_change(monkeypatch):
    import app.knowledge.answer_evaluation as answer_evaluation

    with private_database_root() as database_directory, private_reports_root() as report_directory:
        dataset, database, _ = create_inputs(Path(database_directory))
        args = make_args(dataset, database, Path(report_directory))
        original_hash = answer_evaluation._file_sha256
        calls = 0

        def changing_snapshot_hash(path: Path) -> str:
            nonlocal calls
            digest = original_hash(path)
            if Path(path) == database:
                calls += 1
                return digest if calls == 1 else "0" * 64
            return digest

        monkeypatch.setattr(answer_evaluation, "_file_sha256", changing_snapshot_hash)
        result = asyncio.run(
            answer_evaluation.run_answer_evaluation(args, llm_client=None, run_id=RUN_ID)
        )

        assert result.exit_code == 1
        assert result.manifest.status == "incomplete"
        assert result.report["snapshot_unchanged"] is False
        assert verify_trace_run(result.run_dir).status == "incomplete"


def test_answer_runner_detects_dataset_hash_change(monkeypatch):
    import app.knowledge.answer_evaluation as answer_evaluation

    with private_database_root() as database_directory, private_reports_root() as report_directory:
        dataset, database, _ = create_inputs(Path(database_directory))
        args = make_args(dataset, database, Path(report_directory))
        original_hash = answer_evaluation._file_sha256
        calls = 0

        def changing_dataset_hash(path: Path) -> str:
            nonlocal calls
            digest = original_hash(path)
            if Path(path) == dataset:
                calls += 1
                return digest if calls == 1 else "0" * 64
            return digest

        monkeypatch.setattr(answer_evaluation, "_file_sha256", changing_dataset_hash)
        result = asyncio.run(
            answer_evaluation.run_answer_evaluation(args, llm_client=None, run_id=RUN_ID)
        )

        assert result.exit_code == 1
        assert result.manifest.status == "incomplete"
        assert result.report["dataset_unchanged"] is False
        assert verify_trace_run(result.run_dir).status == "incomplete"