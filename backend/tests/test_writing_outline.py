import json

import pytest

from app.knowledge.service import CitationSnapshot
from app.writing.execution_models import WritingExecutionError
from app.writing.outline import validate_outline


def citation(citation_id: str = "chunk-1", *, text: str = "证据") -> CitationSnapshot:
    return CitationSnapshot(
        citation_id=citation_id,
        chunk_id=citation_id,
        document_id="doc-1",
        document_version="v1",
        source_path="redis.md",
        source_url=None,
        title="Redis",
        heading_path="过期",
        start_line=1,
        end_line=1,
        text=text,
    )


def outline_payload(**overrides):
    payload = {
        "title": "Redis",
        "sections": [{
            "title": "过期",
            "points": ["设置期限"],
            "citation_ids": ["chunk-1"],
        }],
        "gaps": [],
    }
    payload.update(overrides)
    return payload


def assert_error_code(raw: str, citations, code: str, *, max_response_chars: int = 20000):
    with pytest.raises(WritingExecutionError) as error:
        validate_outline(raw, citations, max_response_chars=max_response_chars)
    assert error.value.code == code


def test_valid_outline_and_evidence_reference_are_returned():
    result = validate_outline(json.dumps(outline_payload()), [citation()])

    assert result.title == "Redis"
    assert result.sections[0].citation_ids == ["chunk-1"]


def test_outline_rejects_citations_outside_supplied_evidence():
    raw = json.dumps(outline_payload(sections=[{
        "title": "过期",
        "points": ["设置期限"],
        "citation_ids": ["unknown-chunk"],
    }]))

    assert_error_code(raw, [], "invalid_citation")


@pytest.mark.parametrize("payload", [
    outline_payload(title="  "),
    outline_payload(sections=[]),
    {**outline_payload(), "saved_path": "/tmp/draft.md"},
])
def test_outline_rejects_invalid_schema(payload):
    assert_error_code(json.dumps(payload), [citation()], "invalid_model_response")


def test_outline_rejects_duplicate_json_keys():
    raw = '{"title":"Redis","title":"Other","sections":[],"gaps":[]}'

    assert_error_code(raw, [], "invalid_model_response")


@pytest.mark.parametrize("raw", [
    '{"title":"Redis","sections":[],"gaps":[],"score":NaN}',
    '{"title":"Redis","sections":[],"gaps":[],"score":Infinity}',
])
def test_outline_rejects_non_finite_json_values(raw):
    assert_error_code(raw, [], "invalid_model_response")


def test_outline_rejects_response_over_limit():
    raw = json.dumps(outline_payload())

    assert_error_code(raw, [citation()], "invalid_model_response", max_response_chars=len(raw) - 1)


def test_outline_rejects_duplicate_citation_ids_within_section():
    payload = outline_payload(sections=[{
        "title": "过期",
        "points": ["设置期限"],
        "citation_ids": ["chunk-1", "chunk-1"],
    }])

    assert_error_code(json.dumps(payload), [citation()], "invalid_citation")


def test_outline_rejects_conflicting_snapshots_for_same_citation_id():
    payload = outline_payload()

    assert_error_code(
        json.dumps(payload),
        [citation(text="一版正文"), citation(text="另一版正文")],
        "invalid_citation",
    )