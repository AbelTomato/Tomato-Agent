from datetime import datetime, timezone
from importlib.util import find_spec
from uuid import UUID

import pytest
from pydantic import ValidationError

def valid_span(**changes):
    payload = {
        "schema_version": "runtime-trace/v1",
        "event_id": UUID("00000000-0000-0000-0000-000000000001"),
        "runtime_instance_id": UUID("00000000-0000-0000-0000-000000000002"),
        "sequence": 1,
        "trace_id": UUID("00000000-0000-0000-0000-000000000003"),
        "span_id": UUID("00000000-0000-0000-0000-000000000004"),
        "parent_span_id": None,
        "run_id": UUID("00000000-0000-0000-0000-000000000005"),
        "operation": "model.complete",
        "started_at": datetime(2026, 11, 20, tzinfo=timezone.utc),
        "duration_ms": 12.5,
        "status": "ok",
        "error_code": None,
        "error_category": None,
        "attributes": {"provider_name": "test-provider", "input_tokens": 12},
    }
    payload.update(changes)
    from app.observability.trace_models import RuntimeTraceSpan

    return RuntimeTraceSpan.model_validate(payload)


def test_runtime_trace_span_accepts_versioned_safe_metadata():
    assert find_spec("app.observability.trace_models") is not None
    span = valid_span()

    assert span.schema_version == "runtime-trace/v1"
    assert span.sequence == 1
    assert span.attributes == {"provider_name": "test-provider", "input_tokens": 12}


@pytest.mark.parametrize("field,value", [
    ("prompt", "user text"),
    ("messages", [{"role": "user", "content": "user text"}]),
    ("raw_error", "provider response body"),
])
def test_runtime_trace_span_rejects_unmodeled_content_fields(field, value):
    with pytest.raises(ValidationError):
        valid_span(**{field: value})


def test_runtime_trace_span_rejects_content_attributes():
    with pytest.raises(ValidationError):
        valid_span(attributes={"prompt": "user text"})


@pytest.mark.parametrize("changes", [
    {"sequence": 0},
    {"duration_ms": -0.1},
    {"duration_ms": float("inf")},
    {"started_at": datetime(2026, 11, 20)},
    {"status": "ok", "error_code": "provider_failed", "error_category": "provider"},
    {"status": "error", "error_code": None, "error_category": None},
])
def test_runtime_trace_span_rejects_invalid_boundaries(changes):
    with pytest.raises(ValidationError):
        valid_span(**changes)


def test_runtime_trace_span_rejects_oversized_attribute_values():
    with pytest.raises(ValidationError):
        valid_span(attributes={"model_name": "m" * 129})


@pytest.mark.parametrize("key", ["provider_name", "model_name", "tool_name"])
def test_runtime_trace_span_rejects_non_identifier_metadata(key):
    with pytest.raises(ValidationError):
        valid_span(attributes={key: "model=private secret"})


@pytest.mark.parametrize("attributes", [
    {"model_name": 10},
    {"usage_known": 1},
    {"input_tokens": "10"},
])
def test_runtime_trace_span_enforces_attribute_types(attributes):
    with pytest.raises(ValidationError):
        valid_span(attributes=attributes)
