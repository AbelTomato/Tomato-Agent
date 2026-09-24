from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.observability.rag_trace import (
    TRACE_SCHEMA,
    ArtifactDigest,
    DataFingerprint,
    HumanAnswerReview,
    LLMCallRecord,
    PromptIdentity,
    ReviewStatus,
    TraceEvent,
    TraceRunManifest,
    canonical_json_bytes,
    canonical_json_sha256,
)


RUN_ID = UUID("4f25268d-e611-4d21-a31b-0a8c6e8240cf")
UTC_NOW = datetime(2026, 9, 24, 1, 0, tzinfo=timezone.utc)
GOOD_SHA256 = "a" * 64


def make_fingerprint() -> DataFingerprint:
    return DataFingerprint(
        path_identifier="snapshot/public-blog-v1.db",
        sha256=GOOD_SHA256,
        version="v1",
        manifest_sha256=GOOD_SHA256,
    )


def make_manifest(**overrides) -> TraceRunManifest:
    values = {
        "trace_schema": TRACE_SCHEMA,
        "run_id": RUN_ID,
        "environment": "dev",
        "split": "dev",
        "started_at": UTC_NOW,
        "finished_at": None,
        "status": "running",
        "dataset": make_fingerprint(),
        "snapshot": make_fingerprint(),
        "implementation": {"revision": "abc123"},
        "configuration": {"retrieval_mode": "hybrid", "top_k": 5},
        "artifacts": (),
    }
    values.update(overrides)
    return TraceRunManifest(**values)


def make_event(**overrides) -> TraceEvent:
    values = {
        "schema_version": TRACE_SCHEMA,
        "run_id": RUN_ID,
        "question_id": "blog-dev-001",
        "sequence": 1,
        "occurred_at": UTC_NOW,
        "event_type": "question.started",
        "status": "success",
        "duration_ms": 0.0,
        "payload": {"query": "如何配置 FastAPI？", "evidence_text": "离线审计正文"},
        "previous_event_sha256": None,
        "event_sha256": GOOD_SHA256,
    }
    values.update(overrides)
    return TraceEvent(**values)


def make_prompt() -> PromptIdentity:
    return PromptIdentity(prompt_id="rag.answer", version="1", sha256=GOOD_SHA256)


def make_llm_call(**overrides) -> LLMCallRecord:
    messages = [{"role": "user", "content": "只在受控离线工件中记录"}]
    tools = []
    values = {
        "call_id": "call-001",
        "run_id": RUN_ID,
        "question_id": "blog-dev-001",
        "purpose": "answerer",
        "attempt": 1,
        "model_id": "test-model",
        "prompt": make_prompt(),
        "messages": messages,
        "tools": tools,
        "messages_sha256": canonical_json_sha256(messages),
        "tools_sha256": canonical_json_sha256(tools),
        "status": "success",
        "duration_ms": 10.5,
        "response": {"content": "受控离线答案", "citations": []},
        "error_code": None,
    }
    values.update(overrides)
    return LLMCallRecord(**values)


def test_trace_manifest_is_versioned_dev_only_and_tracks_artifacts():
    artifact = ArtifactDigest(
        relative_path="questions/blog-dev-001.json",
        size_bytes=123,
        sha256=GOOD_SHA256,
    )
    manifest = make_manifest(
        status="complete",
        finished_at=datetime(2026, 9, 24, 1, 1, tzinfo=timezone.utc),
        artifacts=(artifact,),
    )

    assert manifest.trace_schema == "rag-observe-trace/v1"
    assert manifest.environment == manifest.split == "dev"
    assert manifest.artifacts[0].relative_path == "questions/blog-dev-001.json"
    with pytest.raises(ValidationError):
        make_manifest(trace_schema="rag-observe-trace/v2")
    with pytest.raises(ValidationError):
        make_manifest(environment="production")
    with pytest.raises(ValidationError):
        make_manifest(status="complete")
    with pytest.raises(ValidationError):
        make_manifest(
            status="complete",
            finished_at=datetime(2026, 9, 24, 1, 1, tzinfo=timezone.utc),
            artifacts=(
                ArtifactDigest(relative_path="manifest.json", size_bytes=1, sha256=GOOD_SHA256),
            ),
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_id": ""},
        {"question_id": ""},
        {"question_id": "   "},
        {"sequence": 0},
        {"sequence": -1},
        {"duration_ms": -0.1},
        {"duration_ms": float("inf")},
        {"event_sha256": "not-a-sha256"},
        {"previous_event_sha256": "g" * 64},
        {"schema_version": "rag-observe-trace/v2"},
        {"status": "unknown"},
    ],
)
def test_trace_event_rejects_invalid_public_fields(overrides):
    with pytest.raises(ValidationError):
        make_event(**overrides)


def test_trace_event_accepts_controlled_query_and_evidence_content():
    event = make_event()

    assert event.payload["query"] == "如何配置 FastAPI？"
    assert event.payload["evidence_text"] == "离线审计正文"
    with pytest.raises(TypeError):
        event.payload["query"] = "修改后的查询"


@pytest.mark.parametrize("sensitive_name", ["api_key", "accessToken", "authorization", "client_secret"])
def test_trace_contract_rejects_sensitive_field_names_recursively(sensitive_name):
    with pytest.raises(ValidationError):
        make_event(payload={"nested": {sensitive_name: "must-not-be-recorded"}})


def test_prompt_identity_and_llm_call_validate_hashes_and_linkage():
    call = make_llm_call()

    assert call.prompt.prompt_id == "rag.answer"
    assert call.messages[0]["content"] == "只在受控离线工件中记录"
    assert call.tools == []
    with pytest.raises(TypeError):
        call.messages[0]["content"] = "被篡改的内容"
    with pytest.raises(ValidationError):
        PromptIdentity(prompt_id="rag.answer", version="1", sha256="bad")
    with pytest.raises(ValidationError):
        make_llm_call(messages_sha256="b" * 64)
    with pytest.raises(ValidationError):
        make_llm_call(purpose="unknown")
    with pytest.raises(ValidationError):
        make_llm_call(attempt=0)


def test_human_review_keeps_four_quality_dimensions_separate():
    pending = HumanAnswerReview(
        question_id="blog-dev-001",
        reviewer_id=None,
        reviewed_at=None,
        correctness="not_reviewed",
        completeness="not_reviewed",
        groundedness="not_reviewed",
        citation_fidelity="not_reviewed",
        citation_chunk_ids=(),
        rationale=None,
    )
    reviewed = HumanAnswerReview(
        question_id="blog-dev-001",
        reviewer_id="reviewer-17",
        reviewed_at=UTC_NOW,
        correctness="pass",
        completeness="partial",
        groundedness="fail",
        citation_fidelity="not_reviewed",
        citation_chunk_ids=("chunk-a", "chunk-b"),
        rationale="答案覆盖不完整，且第二个结论缺少证据。",
    )

    assert pending.groundedness == "not_reviewed"
    assert reviewed.correctness == "pass"
    assert reviewed.completeness == "partial"
    assert reviewed.groundedness == "fail"
    assert reviewed.citation_fidelity == "not_reviewed"
    with pytest.raises(ValidationError):
        HumanAnswerReview(
            question_id="blog-dev-001",
            reviewer_id=None,
            reviewed_at=None,
            correctness="pass",
            completeness="not_reviewed",
            groundedness="not_reviewed",
            citation_fidelity="not_reviewed",
            citation_chunk_ids=(),
            rationale=None,
        )


@pytest.mark.parametrize("status", ["pass", "partial", "fail", "not_reviewed"])
def test_review_status_contract(status):
    assert status in ReviewStatus.__args__


def test_canonical_json_is_utf8_sorted_compact_and_hash_stable():
    first = {"中文": "证据", "z": [1, True], "a": {"b": 2}}
    second = {"a": {"b": 2}, "z": [1, True], "中文": "证据"}

    assert canonical_json_bytes(first) == b'{"a":{"b":2},"z":[1,true],"\xe4\xb8\xad\xe6\x96\x87":"\xe8\xaf\x81\xe6\x8d\xae"}'
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert canonical_json_sha256(first) == canonical_json_sha256(second)
    assert len(canonical_json_sha256(make_event())) == 64


@pytest.mark.parametrize("value", [{"value": float("nan")}, {"value": float("inf")}, {"value": object()}])
def test_canonical_json_rejects_non_json_and_non_finite_values(value):
    with pytest.raises((TypeError, ValueError)):
        canonical_json_bytes(value)


@pytest.mark.parametrize("value", [("tuple-is-not-json",), {1: "non-string-key"}])
def test_canonical_json_rejects_python_values_outside_json_data_model(value):
    with pytest.raises(ValueError):
        canonical_json_bytes(value)


def test_artifact_paths_and_timestamps_are_validated():
    with pytest.raises(ValidationError):
        ArtifactDigest(relative_path="../outside.json", size_bytes=1, sha256=GOOD_SHA256)
    with pytest.raises(ValidationError):
        make_event(occurred_at=datetime(2026, 9, 24, 1, 0))
    with pytest.raises(ValidationError):
        make_manifest(
            status="incomplete",
            finished_at=datetime(2026, 9, 24, 0, 59, tzinfo=timezone.utc),
        )
