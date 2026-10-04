import pytest

from app.knowledge.service import CitationSnapshot
from app.writing.citation_models import WritingCitation


def citation_payload() -> dict:
    return {
        "citation_id": "chunk-1",
        "chunk_id": "chunk-1",
        "document_id": "doc-1",
        "document_version": "v1",
        "source_path": "redis.md",
        "source_url": None,
        "title": "Redis",
        "heading_path": "过期",
        "start_line": 1,
        "end_line": 2,
        "text": "设置过期时间。",
    }


def test_writing_citation_converts_knowledge_snapshot_without_changing_payload():
    snapshot = CitationSnapshot.model_validate(citation_payload())

    citation = WritingCitation.from_knowledge_snapshot(snapshot)

    assert isinstance(citation, WritingCitation)
    assert citation.model_dump(mode="json") == snapshot.model_dump(mode="json")
    assert citation is not snapshot


def test_writing_citation_accepts_legacy_dict_payload():
    citation = WritingCitation.from_knowledge_snapshot(citation_payload())

    assert citation.citation_id == "chunk-1"
    assert citation.text == "设置过期时间。"


@pytest.mark.parametrize(
    "payload",
    [
        {**citation_payload(), "start_line": 3, "end_line": 2},
        {**citation_payload(), "text": ""},
    ],
)
def test_writing_citation_keeps_evidence_validation(payload):
    with pytest.raises(ValueError):
        WritingCitation.model_validate(payload)