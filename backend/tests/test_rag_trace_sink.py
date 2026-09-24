from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from app.observability import rag_trace_sink
from app.observability.rag_trace import (
    ArtifactDigest,
    DataFingerprint,
    HumanAnswerReview,
    TRACE_SCHEMA,
    TraceRunManifest,
    canonical_json_bytes,
    canonical_json_sha256,
)
from app.observability.rag_trace_sink import (
    RagTraceSink,
    TraceIntegrityError,
    TraceSinkError,
    verify_trace_run,
)


RUN_ID = UUID("4f25268d-e611-4d21-a31b-0a8c6e8240cf")
STARTED_AT = datetime(2026, 9, 24, 1, 0, tzinfo=timezone.utc)
GOOD_SHA256 = "a" * 64


@pytest.fixture
def reports_root():
    """Keep test report artifacts under the project's ignored RAG reports tree."""
    base = rag_trace_sink.DEFAULT_REPORTS_ROOT
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".test-rag-trace-sink-", dir=base) as directory:
        yield Path(directory)


def fingerprint(path_identifier: str) -> DataFingerprint:
    return DataFingerprint(
        path_identifier=path_identifier,
        sha256=GOOD_SHA256,
        version="v1",
        manifest_sha256=GOOD_SHA256,
    )


def make_sink(
    reports_root: Path,
    *,
    question_ids: tuple[str, ...] = ("blog-dev-001", "blog-dev-002"),
    run_id: UUID = RUN_ID,
) -> RagTraceSink:
    return RagTraceSink(
        report_root=reports_root,
        dataset_name="public-blog-v1",
        run_id=run_id,
        expected_question_ids=question_ids,
        started_at=STARTED_AT,
        dataset=fingerprint("dataset/blog_questions.jsonl"),
        snapshot=fingerprint("snapshot/public-blog-v1.db"),
        implementation={"revision": "abc123"},
        configuration={"retrieval_mode": "hybrid", "top_k": 5},
    )


def populate_complete_run(sink: RagTraceSink) -> None:
    for question_id in sink.expected_question_ids:
        sink.write_question(
            question_id,
            {
                "query": f"受控离线问题 {question_id}",
                "answer": "受控离线答案",
                "citations": [],
            },
        )
        sink.append_event(
            question_id=question_id,
            event_type="question.started",
            status="success",
            payload={"query_sha256": GOOD_SHA256},
            duration_ms=0,
            occurred_at=STARTED_AT,
        )


def test_run_directory_uses_fixed_layout_private_permissions_and_exclusive_uuid(reports_root):
    sink = make_sink(reports_root)

    assert sink.run_dir == (
        reports_root
        / "2026-09-24"
        / "public-blog-v1"
        / "dev"
        / "observe-v1"
        / str(RUN_ID)
    )
    assert os.stat(sink.run_dir).st_mode & 0o777 == 0o700
    assert os.stat(sink.manifest_path).st_mode & 0o777 == 0o600
    with pytest.raises(TraceSinkError, match="already exists"):
        make_sink(reports_root)


@pytest.mark.parametrize("invalid_root", [Path("relative/reports"), Path("/tmp/rag-reports")])
def test_report_root_must_be_absolute_and_inside_project_reports(invalid_root):
    with pytest.raises(TraceSinkError):
        RagTraceSink(
            report_root=invalid_root,
            dataset_name="public-blog-v1",
            run_id=RUN_ID,
            expected_question_ids=("blog-dev-001",),
            started_at=STARTED_AT,
            dataset=fingerprint("dataset.jsonl"),
            snapshot=fingerprint("snapshot.db"),
            implementation={},
            configuration={},
        )


def test_report_root_rejects_escape_symlink(reports_root):
    escape = reports_root / "escape"
    escape.symlink_to(rag_trace_sink.DEFAULT_REPORTS_ROOT.parent, target_is_directory=True)

    with pytest.raises(TraceSinkError, match="inside"):
        make_sink(escape)


@pytest.mark.parametrize("dataset_name", ["", ".", "..", "../outside", "nested/name", "with\\backslash"])
def test_dataset_name_is_a_single_safe_path_component(reports_root, dataset_name):
    with pytest.raises(TraceSinkError):
        RagTraceSink(
            report_root=reports_root,
            dataset_name=dataset_name,
            run_id=RUN_ID,
            expected_question_ids=("blog-dev-001",),
            started_at=STARTED_AT,
            dataset=fingerprint("dataset.jsonl"),
            snapshot=fingerprint("snapshot.db"),
            implementation={},
            configuration={},
        )


def test_question_and_review_artifacts_are_separate_private_json_files(reports_root):
    sink = make_sink(reports_root)
    sink.write_question("blog-dev-001", {"query": "离线问题", "answer": "答案"})
    question_files = list((sink.run_dir / "questions").glob("*.json"))

    assert len(question_files) == 1
    assert json.loads(question_files[0].read_text(encoding="utf-8"))["trace"]["query"] == "离线问题"
    assert os.stat(question_files[0]).st_mode & 0o777 == 0o600

    review = HumanAnswerReview(
        question_id="blog-dev-001",
        reviewer_id="reviewer-1",
        reviewed_at=STARTED_AT,
        correctness="pass",
        completeness="partial",
        groundedness="not_reviewed",
        citation_fidelity="not_reviewed",
        citation_chunk_ids=(),
        rationale="保留独立人工判断。",
    )
    sink.write_review(review)
    review_lines = (sink.run_dir / "human-reviews.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(review_lines) == 2
    reviews = [HumanAnswerReview.model_validate_json(line) for line in review_lines]
    assert reviews[0].question_id == "blog-dev-001"
    assert reviews[0].correctness == "pass"
    assert reviews[1].question_id == "blog-dev-002"
    assert reviews[1].correctness == "not_reviewed"


def test_event_jsonl_has_global_sequence_and_verified_hash_chain(reports_root):
    sink = make_sink(reports_root)
    populate_complete_run(sink)
    events = [
        json.loads(line)
        for line in (sink.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert [event["sequence"] for event in events] == [1, 2]
    assert events[0]["previous_event_sha256"] is None
    assert events[1]["previous_event_sha256"] == events[0]["event_sha256"]
    for event in events:
        recorded_hash = event.pop("event_sha256")
        assert canonical_json_sha256(event) == recorded_hash


def test_complete_run_writes_manifest_digests_and_verifies_artifact_set(reports_root):
    sink = make_sink(reports_root)
    populate_complete_run(sink)
    manifest = sink.complete()

    assert manifest.trace_schema == TRACE_SCHEMA
    assert manifest.status == "complete"
    assert manifest.finished_at is not None
    assert all(artifact.relative_path != "manifest.json" for artifact in manifest.artifacts)
    expected_paths = {"events.jsonl", "human-reviews.jsonl", "run-metadata.json"}
    expected_paths.update(
        f"questions/{path.name}" for path in (sink.run_dir / "questions").glob("*.json")
    )
    assert {artifact.relative_path for artifact in manifest.artifacts} == expected_paths
    assert verify_trace_run(sink.run_dir).status == "complete"


def test_artifact_tampering_is_detected(reports_root):
    sink = make_sink(reports_root)
    populate_complete_run(sink)
    sink.complete()
    question_file = next((sink.run_dir / "questions").glob("*.json"))
    original = question_file.read_bytes()
    question_file.write_bytes(original.replace("受控离线答案".encode(), "被篡改离线答案".encode()))

    with pytest.raises(TraceIntegrityError, match="digest|size|artifact"):
        verify_trace_run(sink.run_dir)


def test_event_chain_tampering_is_detected_even_if_artifact_digest_is_recomputed(reports_root):
    sink = make_sink(reports_root)
    populate_complete_run(sink)
    sink.complete()
    event_path = sink.run_dir / "events.jsonl"
    events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    events[1]["previous_event_sha256"] = "0" * 64
    event_path.write_bytes(b"".join(canonical_json_bytes(event) + b"\n" for event in events))

    manifest = TraceRunManifest.model_validate_json(sink.manifest_path.read_bytes())
    event_digest = ArtifactDigest(
        relative_path="events.jsonl",
        size_bytes=event_path.stat().st_size,
        sha256=__import__("hashlib").sha256(event_path.read_bytes()).hexdigest(),
    )
    updated_artifacts = tuple(
        event_digest if artifact.relative_path == "events.jsonl" else artifact
        for artifact in manifest.artifacts
    )
    updated_manifest = manifest.model_copy(update={"artifacts": updated_artifacts})
    sink.manifest_path.write_bytes(canonical_json_bytes(updated_manifest))

    with pytest.raises(TraceIntegrityError, match="chain|previous|event hash"):
        verify_trace_run(sink.run_dir)


def test_missing_question_or_event_prevents_complete_and_preserves_incomplete_status(reports_root):
    sink = make_sink(reports_root)
    sink.write_question("blog-dev-001", {"query": "问题"})

    with pytest.raises(TraceIntegrityError, match="question|event|expected"):
        sink.complete()

    manifest = TraceRunManifest.model_validate_json(sink.manifest_path.read_bytes())
    assert manifest.status == "incomplete"
    assert manifest.finished_at is not None
    assert verify_trace_run(sink.run_dir).status == "incomplete"


def test_context_manager_persists_incomplete_state_and_error_classification(reports_root):
    sink = make_sink(reports_root)
    with pytest.raises(RuntimeError, match="evaluation stopped"):
        with sink as active_sink:
            active_sink.write_question("blog-dev-001", {"query": "受控问题"})
            raise RuntimeError("evaluation stopped")

    manifest = TraceRunManifest.model_validate_json(sink.manifest_path.read_bytes())
    failures = json.loads((sink.run_dir / "failures.json").read_text(encoding="utf-8"))
    assert manifest.status == "incomplete"
    assert failures == [{"code": "evaluation_error"}]
    assert verify_trace_run(sink.run_dir).status == "incomplete"


def test_explicit_failure_classification_keeps_completed_artifacts(reports_root):
    sink = make_sink(reports_root)
    sink.write_question("blog-dev-001", {"query": "已完成题"})
    sink.mark_incomplete("model_call_failed")

    manifest = TraceRunManifest.model_validate_json(sink.manifest_path.read_bytes())
    assert manifest.status == "incomplete"
    assert {artifact.relative_path for artifact in manifest.artifacts} >= {
        "questions/" + next(path.name for path in (sink.run_dir / "questions").glob("*.json")),
        "failures.json",
    }
    assert json.loads((sink.run_dir / "failures.json").read_text(encoding="utf-8")) == [
        {"code": "model_call_failed"}
    ]


def test_additional_artifact_is_private_listed_and_cannot_be_overwritten(reports_root):
    sink = make_sink(reports_root)
    artifact = sink.write_artifact("summary.json", '{"split":"dev"}')
    with pytest.raises(TraceSinkError, match="already exists"):
        sink.write_artifact("summary.json", "replacement")
    populate_complete_run(sink)
    manifest = sink.complete()

    summary_digest = next(item for item in manifest.artifacts if item.relative_path == "summary.json")
    assert artifact.read_text(encoding="utf-8") == '{"split":"dev"}'
    assert summary_digest.size_bytes == artifact.stat().st_size
    assert summary_digest.sha256 == __import__("hashlib").sha256(artifact.read_bytes()).hexdigest()
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert verify_trace_run(sink.run_dir).status == "complete"


def test_additional_artifact_rejects_path_traversal_and_reserved_paths(reports_root):
    sink = make_sink(reports_root)
    for path in ("../outside.json", "nested/summary.json", "manifest.json", "../manifest.json"):
        with pytest.raises(TraceSinkError):
            sink.write_artifact(path, "not-written")
    with pytest.raises(TraceSinkError, match="safe JSON"):
        sink.write_artifact("summary.json", '{"authorization":"must-not-persist"}')
    assert not (sink.run_dir / "summary.json").exists()


def test_completed_run_can_be_downgraded_after_late_verification_failure(reports_root):
    sink = make_sink(reports_root)
    populate_complete_run(sink)
    sink.write_artifact("summary.json", '{"split":"dev"}')
    sink.complete()

    incomplete = sink.mark_incomplete("artifact_integrity_failed")

    assert incomplete.status == "incomplete"
    assert verify_trace_run(sink.run_dir).status == "incomplete"
    assert json.loads((sink.run_dir / "failures.json").read_text(encoding="utf-8")) == [
        {"code": "artifact_integrity_failed"}
    ]


def test_sensitive_question_payload_is_rejected_without_persisting_secret(reports_root):
    sink = make_sink(reports_root)

    with pytest.raises((TraceSinkError, ValueError)):
        sink.write_question("blog-dev-001", {"query": "问题", "api_key": "do-not-save"})

    assert b"do-not-save" not in sink.manifest_path.read_bytes()
    assert not list((sink.run_dir / "questions").glob("*.json"))


def test_duplicate_or_unknown_question_ids_are_rejected(reports_root):
    sink = make_sink(reports_root)

    with pytest.raises(TraceSinkError, match="expected|unknown"):
        sink.write_question("not-in-dataset", {"query": "问题"})
    sink.write_question("blog-dev-001", {"query": "问题"})
    with pytest.raises(TraceSinkError, match="duplicate|already"):
        sink.write_question("blog-dev-001", {"query": "覆盖"})


def test_write_error_marks_run_incomplete_and_preserves_safe_error_code(reports_root, monkeypatch):
    sink = make_sink(reports_root)
    original_atomic_write = sink._atomic_write
    failed_once = False

    def fail_question_write(path: Path, data: bytes) -> None:
        nonlocal failed_once
        if path.parent.name == "questions" and not failed_once:
            failed_once = True
            raise OSError("private filesystem details must not enter the trace")
        original_atomic_write(path, data)

    monkeypatch.setattr(sink, "_atomic_write", fail_question_write)
    with pytest.raises(OSError, match="private filesystem details"):
        sink.write_question("blog-dev-001", {"query": "问题"})

    manifest = TraceRunManifest.model_validate_json(sink.manifest_path.read_bytes())
    failures = json.loads((sink.run_dir / "failures.json").read_text(encoding="utf-8"))
    assert manifest.status == "incomplete"
    assert failures == [{"code": "artifact_write_failed"}]
    assert "private filesystem details" not in (sink.run_dir / "failures.json").read_text()


def test_invalid_event_hash_sequence_and_payload_cannot_be_written(reports_root):
    sink = make_sink(reports_root)
    sink.write_question("blog-dev-001", {"query": "问题"})
    sink.append_event(
        question_id="blog-dev-001",
        event_type="question.started",
        status="success",
        payload={"query_sha256": GOOD_SHA256},
        duration_ms=0,
        occurred_at=STARTED_AT,
    )

    with pytest.raises(TraceSinkError, match="expected|unknown"):
        sink.append_event(
            question_id="unknown",
            event_type="question.started",
            status="success",
            payload={},
        )
    with pytest.raises((TraceSinkError, ValueError)):
        sink.append_event(
            question_id="blog-dev-002",
            event_type="question.started",
            status="success",
            payload={"authorization": "never persist"},
        )


def test_verify_rejects_unmanifested_files(reports_root):
    sink = make_sink(reports_root)
    populate_complete_run(sink)
    sink.complete()
    (sink.run_dir / "unexpected.json").write_text("{}", encoding="utf-8")

    with pytest.raises(TraceIntegrityError, match="unexpected|artifact set"):
        verify_trace_run(sink.run_dir)