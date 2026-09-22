from pathlib import Path

import pytest

from app.knowledge.ingestion import MarkdownParseError, parse_markdown


def test_parse_markdown_tracks_heading_path_lines_and_ignores_fenced_headings():
    text = """# Root

Intro paragraph.

## Setup

Use `redis-cli`.

```python
# not a heading
print('hello')
```

### Details

More details.
"""

    chunks = parse_markdown(text, target_tokens=500, max_tokens=800)

    assert [chunk.heading_path for chunk in chunks] == [
        "Root",
        "Root > Setup",
        "Root > Setup > Details",
    ]
    assert chunks[0].start_line == 1
    assert chunks[1].start_line == 5
    assert chunks[1].end_line == 12
    assert "# not a heading" in chunks[1].text
    assert chunks[1].token_count > 0


def test_parse_markdown_keeps_large_code_block_as_one_chunk():
    text = "# Code\n\n```text\nline one\nline two\n```\n"

    chunks = parse_markdown(text, target_tokens=1, max_tokens=2_000)

    assert len(chunks) == 1
    assert chunks[0].text.startswith("```text")
    assert "line two" in chunks[0].text


def test_parse_markdown_rejects_code_block_over_two_thousand_tokens():
    text = "# Code\n\n```text\n" + ("word " * 2_100) + "\n```\n"

    with pytest.raises(MarkdownParseError, match="2,000"):
        parse_markdown(text)


def test_read_markdown_rejects_invalid_utf8(tmp_path: Path):
    path = tmp_path / "invalid.md"
    path.write_bytes(b"# invalid\n\xff")

    from app.knowledge.ingestion import read_markdown

    with pytest.raises(MarkdownParseError, match="UTF-8"):
        read_markdown(path)


def test_parse_markdown_empty_document_returns_no_chunks():
    assert parse_markdown("\n\n") == []


def test_parse_markdown_excludes_yaml_front_matter_and_preserves_chinese_heading_line_and_tilde_fence():
    text = """---
title: ignored front matter
---

# 中文标题

第一段内容。

~~~python
# 围栏中的标题
print("测试")
~~~
"""

    chunks = parse_markdown(text)

    assert len(chunks) == 1
    assert chunks[0].heading_path == "中文标题"
    assert chunks[0].start_line == 5
    assert chunks[0].end_line == 12
    assert "title: ignored front matter" not in chunks[0].text
    assert "# 围栏中的标题" in chunks[0].text


def test_parse_markdown_splits_ordinary_text_over_max_tokens():
    chunks = parse_markdown("# Long\n\n" + ("word " * 1_000), target_tokens=500, max_tokens=800)

    assert len(chunks) >= 2
    assert all(chunk.token_count <= 800 for chunk in chunks)


def test_ingest_rejects_symlink_escaping_the_knowledge_root(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n\nsecret", encoding="utf-8")
    link = root / "linked.md"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    from app.knowledge.ingestion import ingest_manifest
    from app.knowledge.repository import KnowledgeRepository

    manifest = root / "manifest.json"
    manifest.write_text(
        '[{"path":"linked.md","title":"Linked","url":null}]', encoding="utf-8"
    )
    repository = KnowledgeRepository(tmp_path / "knowledge.db")

    import asyncio

    asyncio.run(repository.init())
    report = asyncio.run(ingest_manifest(repository, manifest, root))

    assert report.failed == 1
    assert "escapes root" in report.failures[0]