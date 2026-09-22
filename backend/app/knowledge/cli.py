import argparse
import asyncio
from pathlib import Path

from app.knowledge.embeddings import EmbeddingClient, EmbeddingProvider
from app.knowledge.indexing import build_embedding_index
from app.knowledge.ingestion import MarkdownParseError, _load_manifest, ingest_manifest
from app.knowledge.repository import KnowledgeRepository
from app.settings import settings


async def run_ingest(args: argparse.Namespace) -> int:
    repository = KnowledgeRepository(Path(args.database))
    await repository.init()
    report = await ingest_manifest(repository, Path(args.manifest), Path(args.root))
    print(
        f"succeeded={report.succeeded} skipped={report.skipped} "
        f"updated={report.updated} failed={report.failed}"
    )
    for failure in report.failures:
        print(f"failed: {failure}")
    return 1 if report.failed else 0


async def run_index(
    args: argparse.Namespace,
    provider: EmbeddingProvider | None = None,
) -> int:
    model = str(args.embedding_model).strip()
    dimensions = int(args.embedding_dimensions)
    if provider is None:
        if not settings.embedding_api_key.strip():
            raise ValueError("index requires embedding_api_key in settings")
        provider = EmbeddingClient(
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
            model=model,
            dimensions=dimensions,
            timeout_seconds=settings.embedding_timeout_seconds,
        )
    repository = KnowledgeRepository(Path(args.database))
    await repository.init()
    report = await build_embedding_index(
        repository,
        provider,
        model=model,
        dimensions=dimensions,
        batch_size=int(getattr(args, "batch_size", 32)),
    )
    print(
        f"documents={report.documents_seen} indexed={report.indexed_documents} "
        f"skipped={report.skipped_documents} failed={report.failed_documents} "
        f"chunks={report.chunks_indexed}"
    )
    for failure in report.failures:
        print(f"failed: {failure}")
    return 1 if report.failed_documents else 0


async def run_sync(args: argparse.Namespace) -> int:
    repository = KnowledgeRepository(Path(args.database))
    await repository.init()
    manifest_paths = {entry["path"] for entry in _load_manifest(Path(args.manifest)) if isinstance(entry, dict) and isinstance(entry.get("path"), str)}
    candidates = [document.source_path for document in await repository.list_documents() if document.source_path not in manifest_paths]
    for path in candidates:
        print(f"candidate-remove: {path}")
    if args.dry_run:
        return 0
    if candidates and not args.confirm_remove:
        raise MarkdownParseError("sync removal requires --confirm-remove")
    removed = await repository.delete_documents(candidates)
    print(f"removed={removed}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tomato Agent knowledge base")
    parser.add_argument("--database", default=str(settings.database_path))
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("ingest", "sync", "index"):
        subparser = commands.add_parser(command)
        if command in {"ingest", "sync"}:
            subparser.add_argument("--manifest", required=True)
            subparser.add_argument("--root", required=True)
        if command == "sync":
            subparser.add_argument("--dry-run", action="store_true")
            subparser.add_argument("--confirm-remove", action="store_true")
        if command == "index":
            subparser.add_argument("--embedding-model", default=settings.embedding_model)
            subparser.add_argument("--embedding-dimensions", type=int, default=settings.embedding_dimensions)
            subparser.add_argument("--batch-size", type=int, default=32)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "ingest":
            return asyncio.run(run_ingest(args))
        if args.command == "sync":
            return asyncio.run(run_sync(args))
        return asyncio.run(run_index(args))
    except (MarkdownParseError, ValueError) as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())