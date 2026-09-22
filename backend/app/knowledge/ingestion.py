import json
import re
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse

import tiktoken

from app.knowledge.models import Chunk, Document, IngestReport, ParsedChunk, stable_hash
from app.knowledge.repository import KnowledgeRepository


HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
FENCE_PATTERN = re.compile(r"^\s*(`{3,}|~{3,})")
MAX_CODE_BLOCK_TOKENS = 2_000


class MarkdownParseError(ValueError):
    pass


def _token_count(text: str) -> int:
    return len(tiktoken.get_encoding("cl100k_base").encode(text))


def read_markdown(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise MarkdownParseError(f"Markdown document must be UTF-8: {path}") from exc


def _split_section(
    heading_path: str,
    heading_line: int,
    lines: list[tuple[int, str]],
    target_tokens: int,
    max_tokens: int,
) -> list[ParsedChunk]:
    while lines and not lines[0][1].strip():
        lines.pop(0)
    while lines and not lines[-1][1].strip():
        lines.pop()
    if not lines:
        return []

    blocks: list[list[tuple[int, str]]] = []
    block: list[tuple[int, str]] = []
    in_fence = False
    fence_marker = ""
    for line_number, line in lines:
        fence = FENCE_PATTERN.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                if block:
                    blocks.append(block)
                    block = []
                in_fence = True
                fence_marker = marker[0]
                block.append((line_number, line))
                continue
            if marker[0] == fence_marker:
                block.append((line_number, line))
                blocks.append(block)
                block = []
                in_fence = False
                fence_marker = ""
                continue
        if in_fence:
            block.append((line_number, line))
        elif not line.strip():
            if block:
                blocks.append(block)
                block = []
        else:
            block.append((line_number, line))
    if block:
        blocks.append(block)

    chunks: list[ParsedChunk] = []
    current: list[list[tuple[int, str]]] = []
    for block in blocks:
        block_text = "\n".join(line for _, line in block)
        block_tokens = _token_count(block_text)
        is_code = block[0][1].lstrip().startswith(("```", "~~~"))
        if is_code and block_tokens > MAX_CODE_BLOCK_TOKENS:
            raise MarkdownParseError(
                f"code block at line {block[0][0]} exceeds 2,000 tokens"
            )
        current_text = "\n\n".join("\n".join(line for _, line in item) for item in current)
        if current and _token_count(current_text + "\n\n" + block_text) > target_tokens:
            chunks.append(_make_chunk(heading_path, heading_line, current))
            current = []
        if not current and block_tokens > max_tokens and not is_code:
            words = block_text.split()
            segment: list[str] = []
            for word in words:
                if segment and _token_count(" ".join(segment + [word])) > max_tokens:
                    chunks.append(
                        ParsedChunk(
                            heading_path=heading_path,
                            start_line=block[0][0],
                            end_line=block[-1][0],
                            text=" ".join(segment),
                            token_count=_token_count(" ".join(segment)),
                        )
                    )
                    segment = []
                segment.append(word)
            if segment:
                current = [[(block[0][0], " ".join(segment))]]
        else:
            current.append(block)
    if current:
        chunks.append(_make_chunk(heading_path, heading_line, current))
    return chunks


def _make_chunk(
    heading_path: str, heading_line: int, blocks: list[list[tuple[int, str]]]
) -> ParsedChunk:
    text = "\n\n".join("\n".join(line for _, line in block) for block in blocks)
    return ParsedChunk(
        heading_path=heading_path,
        start_line=heading_line,
        end_line=blocks[-1][-1][0],
        text=text,
        token_count=_token_count(text),
    )


def parse_markdown(
    text: str, target_tokens: int = 500, max_tokens: int = 800
) -> list[ParsedChunk]:
    if target_tokens <= 0 or max_tokens < target_tokens:
        raise ValueError("target_tokens must be positive and no greater than max_tokens")
    if not text.strip():
        return []

    source_lines = text.splitlines()
    front_matter_end = 0
    if source_lines and source_lines[0].strip() == "---":
        for index, line in enumerate(source_lines[1:], start=2):
            if line.strip() == "---":
                front_matter_end = index
                break

    sections: list[tuple[str, int, list[tuple[int, str]]]] = []
    headings: list[tuple[int, str]] = []
    current_lines: list[tuple[int, str]] = []
    current_path = ""
    current_heading_line = 1
    in_fence = False
    fence_marker = ""

    for line_number, line in enumerate(source_lines, start=1):
        if line_number <= front_matter_end:
            continue
        fence = FENCE_PATTERN.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = marker[0]
            elif marker[0] == fence_marker:
                in_fence = False
                fence_marker = ""
            current_lines.append((line_number, line))
            continue
        heading = None if in_fence else HEADING_PATTERN.match(line)
        if heading:
            if current_path or current_lines:
                sections.append((current_path, current_heading_line, current_lines))
            level = len(heading.group(1))
            title = heading.group(2).strip().rstrip("#").strip()
            headings = [item for item in headings if item[0] < level]
            headings.append((level, title))
            current_path = " > ".join(item[1] for item in headings)
            current_heading_line = line_number
            current_lines = []
        else:
            current_lines.append((line_number, line))
    if current_path or current_lines:
        sections.append((current_path, current_heading_line, current_lines))

    return [
        chunk
        for heading_path, heading_line, lines in sections
        for chunk in _split_section(
            heading_path, heading_line, lines, target_tokens, max_tokens
        )
    ]


def _load_manifest(path: Path) -> list[dict[str, object]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarkdownParseError(f"invalid manifest: {path}") from exc
    if not isinstance(value, list):
        raise MarkdownParseError("manifest must be a JSON array")
    return value


def _validate_entry(entry: object, root: Path, seen_paths: set[str]) -> tuple[str, str, str | None, Path]:
    if not isinstance(entry, dict):
        raise MarkdownParseError("manifest entry must be an object")
    source_path, title, url = entry.get("path"), entry.get("title"), entry.get("url")
    if not isinstance(source_path, str) or not source_path:
        raise MarkdownParseError("manifest path must be a non-empty string")
    if Path(source_path).suffix.lower() not in {".md", ".markdown"}:
        raise MarkdownParseError(f"only Markdown files are supported: {source_path}")
    if not isinstance(title, str) or not title:
        raise MarkdownParseError("manifest title must be a non-empty string")
    if source_path in seen_paths:
        raise MarkdownParseError(f"duplicate manifest path: {source_path}")
    seen_paths.add(source_path)
    if url is not None:
        if not isinstance(url, str):
            raise MarkdownParseError("manifest url must be a string or null")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MarkdownParseError(f"manifest url must use http or https: {url}")
    candidate = (root / source_path).resolve()
    if candidate == root or root not in candidate.parents:
        raise MarkdownParseError(f"manifest path escapes root: {source_path}")
    if not candidate.is_file():
        raise MarkdownParseError(f"manifest document does not exist: {source_path}")
    return source_path, title, url, candidate


async def ingest_manifest(
    repository: KnowledgeRepository,
    manifest_path: Path,
    root: Path,
    *,
    target_tokens: int = 500,
    max_tokens: int = 800,
) -> IngestReport:
    root = root.resolve()
    if not root.is_dir():
        raise MarkdownParseError(f"knowledge root does not exist: {root}")
    entries = _load_manifest(manifest_path)
    seen_paths: set[str] = set()
    report = IngestReport()
    for entry in entries:
        source_path = "<unknown>"
        try:
            source_path, title, url, source_file = _validate_entry(entry, root, seen_paths)
            content = read_markdown(source_file)
            parsed_chunks = parse_markdown(content, target_tokens, max_tokens)
            if not parsed_chunks:
                raise MarkdownParseError(f"Markdown document is empty: {source_path}")
            content_hash = sha256(content.encode("utf-8")).hexdigest()
            existing = await repository.get_document_by_path(source_path)
            report.succeeded += 1
            if existing is not None and existing.content_hash == content_hash:
                report.skipped += 1
                continue
            document_id = stable_hash("knowledge", source_path)
            document = Document(document_id, source_path, url, title, content_hash)
            chunks = [
                Chunk(
                    chunk_id=stable_hash(
                        document_id,
                        content_hash,
                        parsed.heading_path,
                        str(index),
                    ),
                    document_id=document_id,
                    document_version=content_hash,
                    heading_path=parsed.heading_path,
                    start_line=parsed.start_line,
                    end_line=parsed.end_line,
                    text=parsed.text,
                    token_count=parsed.token_count,
                )
                for index, parsed in enumerate(parsed_chunks)
            ]
            await repository.replace_document(document, chunks)
            if existing is not None:
                report.updated += 1
        except (MarkdownParseError, OSError, ValueError) as exc:
            report.failed += 1
            report.failures.append(f"{source_path}: {exc}")
    return report