import json
from pathlib import Path
from urllib.parse import urlparse

import pytest


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "knowledge"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"


def load_manifest(path: Path) -> list[dict[str, object]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("manifest must be a JSON array")
    return data


def validate_manifest(entries: list[dict[str, object]], root: Path) -> None:
    paths: set[str] = set()
    root = root.resolve()

    for entry in entries:
        path = entry.get("path")
        title = entry.get("title")
        url = entry.get("url")
        if not isinstance(path, str) or not path:
            raise ValueError("manifest path must be a non-empty string")
        if not isinstance(title, str) or not title:
            raise ValueError("manifest title must be a non-empty string")
        if path in paths:
            raise ValueError(f"duplicate manifest path: {path}")
        paths.add(path)

        candidate = (root / path).resolve()
        if candidate == root or root not in candidate.parents:
            raise ValueError(f"manifest path escapes root: {path}")
        if not candidate.is_file():
            raise ValueError(f"manifest document does not exist: {path}")

        if url is not None:
            if not isinstance(url, str):
                raise ValueError("manifest url must be a string or null")
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"manifest url must use http or https: {url}")


def validate_relevant_spans(
    spans: list[dict[str, object]], document_line_counts: dict[str, int]
) -> None:
    for span in spans:
        document_id = span.get("document_id")
        version = span.get("document_version")
        start_line = span.get("start_line")
        end_line = span.get("end_line")
        if not isinstance(document_id, str) or not document_id:
            raise ValueError("relevant span document_id must be a non-empty string")
        if not isinstance(version, str) or not version:
            raise ValueError("relevant span document_version must be a non-empty string")
        if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1:
            raise ValueError("relevant span start_line must be a positive integer")
        if isinstance(end_line, bool) or not isinstance(end_line, int) or end_line < start_line:
            raise ValueError("relevant span end_line must not precede start_line")
        max_line = document_line_counts.get(document_id)
        if max_line is None or end_line > max_line:
            raise ValueError("relevant span line range is outside its document")


def test_fixed_manifest_and_corpus_are_valid():
    entries = load_manifest(MANIFEST_PATH)

    validate_manifest(entries, FIXTURE_ROOT)

    assert [entry["path"] for entry in entries] == ["redis.md", "cache.md"]
    assert "SETEX" in (FIXTURE_ROOT / "redis.md").read_text(encoding="utf-8")
    assert "cachetools.TTLCache" in (FIXTURE_ROOT / "cache.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        (
            [
                {"path": "redis.md", "title": "Redis", "url": None},
                {"path": "redis.md", "title": "重复", "url": None},
            ],
            "duplicate manifest path",
        ),
        (
            [{"path": "../outside.md", "title": "越界", "url": None}],
            "escapes root",
        ),
        (
            [{"path": "redis.md", "title": "Redis", "url": "file:///tmp/redis.md"}],
            "must use http or https",
        ),
        (
            [{"path": "missing.md", "title": "缺失", "url": None}],
            "does not exist",
        ),
    ],
)
def test_manifest_rejects_invalid_entries(
    entries: list[dict[str, object]], message: str
):
    with pytest.raises(ValueError, match=message):
        validate_manifest(entries, FIXTURE_ROOT)


@pytest.mark.parametrize(
    "spans",
    [
        [
            {
                "document_id": "redis",
                "document_version": "v1",
                "start_line": 0,
                "end_line": 2,
            }
        ],
        [
            {
                "document_id": "redis",
                "document_version": "v1",
                "start_line": 5,
                "end_line": 4,
            }
        ],
        [
            {
                "document_id": "redis",
                "document_version": "v1",
                "start_line": 1,
                "end_line": 11,
            }
        ],
    ],
)
def test_relevant_spans_reject_invalid_line_ranges(spans: list[dict[str, object]]):
    with pytest.raises(ValueError, match="line"):
        validate_relevant_spans(spans, {"redis": 10})


def test_relevant_spans_accept_document_version_and_inclusive_line_range():
    validate_relevant_spans(
        [
            {
                "document_id": "redis",
                "document_version": "fixture-v1",
                "start_line": 3,
                "end_line": 8,
            }
        ],
        {"redis": 10},
    )