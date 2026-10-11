import pytest

from app.assistant.completion import validate_completion
from app.assistant.models import AssistantResult
from test_assistant_persistence import assistant_snapshot
from uuid import uuid4


def test_plain_answer_needs_no_validation():
    result = validate_completion(assistant_snapshot(uuid4()), "hello")
    assert result.answer == "hello" and result.validation_status == "not_required"


def test_unobserved_citation_is_rejected():
    citation = {"citation_id": "fake", "chunk_id": "fake", "document_id": "doc", "document_version": "v1",
                "source_path": "a.md", "title": "a", "heading_path": "", "start_line": 1, "end_line": 1, "text": "text"}
    with pytest.raises(ValueError, match="invalid_citation"):
        validate_completion(assistant_snapshot(uuid4()), AssistantResult(answer="claim", citations=[citation]))


def test_read_only_cannot_claim_passed():
    with pytest.raises(ValueError, match="validation_not_available"):
        validate_completion(assistant_snapshot(uuid4()), AssistantResult(answer="passed", validation_status="passed"))