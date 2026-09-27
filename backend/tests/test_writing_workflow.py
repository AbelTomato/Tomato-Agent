import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from app.knowledge.service import CitationSnapshot
from app.sessions.repository import SessionRepository
from app.writing.models import WritingStatus
from app.writing.execution_models import WritingExecutionError
from app.writing.repository import WritingRepository
from app.writing.service import WritingConflictError, WritingService


@pytest.fixture
def citation() -> CitationSnapshot:
    return citation_snapshot()


def citation_snapshot() -> CitationSnapshot:
    return CitationSnapshot(
        citation_id="chunk-1",
        chunk_id="chunk-1",
        document_id="doc-1",
        document_version="v1",
        source_path="redis.md",
        source_url=None,
        title="Redis",
        heading_path="Redis > SETEX",
        start_line=1,
        end_line=3,
        text="SETEX 设置过期时间。",
    )


def confirmed_outline(citation_id: str = "chunk-1") -> dict:
    return {
        "title": "Redis 缓存实践",
        "sections": [{
            "title": "过期策略",
            "points": ["设置过期时间"],
            "citation_ids": [citation_id],
        }],
        "gaps": [],
    }


@pytest.mark.asyncio
async def test_outline_confirmation_is_server_controlled_and_versioned(
    tmp_path: Path, citation: CitationSnapshot
):
    session_repo = SessionRepository(tmp_path / "agent.db")
    await session_repo.init()
    session_id = await session_repo.create_session()
    writing_repo = WritingRepository(tmp_path / "agent.db")
    await writing_repo.init()
    service = WritingService(writing_repo)

    task = await service.create_task(session_id, "Redis 缓存实践")
    assert task.status == "researching"
    assert task.version == 1

    task = await service.publish_outline(
        task.task_id,
        outline={"title": "Redis 缓存实践", "sections": ["过期策略"]},
        citations=[citation],
        research_run_id=uuid4(),
    )
    assert task.status == "awaiting_outline_confirmation"
    assert task.version == 2

    confirmed = await service.confirm_outline(
        task.task_id,
        expected_version=task.version,
        outline=confirmed_outline(),
    )
    assert confirmed.status == "drafting"
    assert confirmed.version == 3
    assert confirmed.outline == confirmed_outline()


@pytest.mark.asyncio
async def test_stale_outline_confirmation_returns_conflict_without_mutation(
    tmp_path: Path,
):
    session_repo = SessionRepository(tmp_path / "agent.db")
    await session_repo.init()
    session_id = await session_repo.create_session()
    writing_repo = WritingRepository(tmp_path / "agent.db")
    await writing_repo.init()
    service = WritingService(writing_repo)

    task = await service.create_task(session_id, "SQLite 事务")
    task = await service.publish_outline(task.task_id, {"sections": ["事务"]}, [])

    with pytest.raises(WritingConflictError):
        await service.confirm_outline(
            task.task_id,
            expected_version=task.version - 1,
            outline=confirmed_outline(),
        )

    current = await service.get_task(task.task_id)
    assert current.status == WritingStatus.AWAITING_OUTLINE_CONFIRMATION
    assert current.version == task.version
    assert current.outline == {"sections": ["事务"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({**confirmed_outline("unknown-chunk")}, "invalid_citation"),
        ({
            **confirmed_outline(),
            "sections": [{
                "title": "过期策略",
                "points": ["设置过期时间"],
                "citation_ids": ["chunk-1", "chunk-1"],
            }],
        }, "invalid_citation"),
        ({**confirmed_outline(), "saved_path": "/tmp/escape.md"}, "invalid_model_response"),
        ({"title": "Redis", "sections": ["过期策略"], "gaps": []}, "invalid_model_response"),
    ],
)
async def test_outline_confirmation_rejects_untrusted_or_invalid_payloads(
    tmp_path: Path, citation: CitationSnapshot, payload: dict, code: str
):
    session_repo = SessionRepository(tmp_path / "agent.db")
    await session_repo.init()
    session_id = await session_repo.create_session()
    writing_repo = WritingRepository(tmp_path / "agent.db")
    await writing_repo.init()
    service = WritingService(writing_repo)

    task = await service.create_task(session_id, "Redis 缓存实践")
    task = await service.publish_outline(task.task_id, confirmed_outline(), [citation])

    with pytest.raises(WritingExecutionError) as error:
        await service.confirm_outline(
            task.task_id,
            expected_version=task.version,
            outline=payload,
        )

    assert error.value.code == code
    current = await service.get_task(task.task_id)
    assert current.status == WritingStatus.AWAITING_OUTLINE_CONFIRMATION
    assert current.version == task.version
    assert current.outline == task.outline


@pytest.mark.asyncio
async def test_draft_publication_requires_confirmed_outline_and_preserves_evidence(
    tmp_path: Path, citation: CitationSnapshot
):
    session_repo = SessionRepository(tmp_path / "agent.db")
    await session_repo.init()
    session_id = await session_repo.create_session()
    writing_repo = WritingRepository(tmp_path / "agent.db")
    await writing_repo.init()
    service = WritingService(writing_repo)

    task = await service.create_task(session_id, "Redis 缓存实践")
    task = await service.publish_outline(
        task.task_id,
        {"sections": ["过期策略"]},
        [citation],
        research_run_id=uuid4(),
    )
    task = await service.confirm_outline(
        task.task_id,
        expected_version=task.version,
        outline=confirmed_outline(),
    )
    drafting_run_id = uuid4()

    published = await service.publish_draft(
        task.task_id,
        expected_version=task.version,
        draft="这是经过确认提纲生成的草稿。",
        drafting_run_id=drafting_run_id,
    )

    assert published.status == WritingStatus.AWAITING_SAVE_CONFIRMATION
    assert published.version == task.version + 1
    assert published.draft == "这是经过确认提纲生成的草稿。"
    assert published.drafting_run_id == drafting_run_id
    assert published.outline == confirmed_outline()
    assert published.citations == [citation]

    with pytest.raises(WritingConflictError):
        await service.publish_draft(
            published.task_id,
            expected_version=published.version,
            draft="不应重复生成",
            drafting_run_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_save_is_idempotent_and_uses_configured_directory(
    tmp_path: Path, citation: CitationSnapshot
):
    session_repo = SessionRepository(tmp_path / "agent.db")
    await session_repo.init()
    session_id = await session_repo.create_session()
    writing_repo = WritingRepository(tmp_path / "agent.db")
    await writing_repo.init()
    service = WritingService(writing_repo, draft_directory=tmp_path / "drafts")

    task = await service.create_task(session_id, "Redis 缓存实践")
    task = await service.publish_outline(task.task_id, {"sections": ["过期"]}, [citation])
    task = await service.confirm_outline(
        task.task_id, expected_version=task.version, outline=confirmed_outline()
    )
    task = await service.publish_draft(
        task.task_id,
        expected_version=task.version,
        draft="可保存的草稿。",
        drafting_run_id=uuid4(),
    )

    saved = await service.save(
        task.task_id,
        expected_version=task.version,
        idempotency_key="save-1",
    )
    repeated = await service.save(
        task.task_id,
        expected_version=task.version,
        idempotency_key="save-1",
    )

    assert saved.status == WritingStatus.SAVED
    assert repeated == saved
    assert saved.saved_path is not None
    assert Path(saved.saved_path).parent == tmp_path / "drafts"
    assert Path(saved.saved_path).read_text(encoding="utf-8") == "可保存的草稿。"
    assert len(list((tmp_path / "drafts").glob("*.md"))) == 1

    with pytest.raises(WritingConflictError):
        await service.save(
            task.task_id,
            expected_version=task.version,
            idempotency_key="different-key",
        )


@pytest.mark.asyncio
async def test_only_read_only_generation_failures_can_be_retried_after_reload(
    tmp_path: Path,
):
    session_repo = SessionRepository(tmp_path / "agent.db")
    await session_repo.init()
    session_id = await session_repo.create_session()
    writing_repo = WritingRepository(tmp_path / "agent.db")
    await writing_repo.init()
    service = WritingService(writing_repo)

    task = await service.create_task(session_id, "SQLite 事务")
    failed = await service.fail(
        task.task_id,
        expected_version=task.version,
        failed_stage="researching",
    )
    assert failed.status == WritingStatus.FAILED
    assert failed.failed_stage == "researching"

    reloaded_repository = WritingRepository(tmp_path / "agent.db")
    await reloaded_repository.init()
    reloaded_service = WritingService(reloaded_repository)
    recovered = await reloaded_service.retry(
        task.task_id,
        expected_version=failed.version,
    )
    assert recovered.status == WritingStatus.RESEARCHING
    assert recovered.failed_stage is None
    assert recovered.version == failed.version + 1

    failed_save = await reloaded_service.fail(
        recovered.task_id,
        expected_version=recovered.version,
        failed_stage="saving",
    )
    assert failed_save.status == WritingStatus.FAILED
    with pytest.raises(WritingConflictError):
        await reloaded_service.retry(
            failed_save.task_id,
            expected_version=failed_save.version,
        )


@pytest.mark.asyncio
async def test_concurrent_outline_confirmations_only_allow_one_transition(tmp_path: Path):
    session_repo = SessionRepository(tmp_path / "agent.db")
    await session_repo.init()
    session_id = await session_repo.create_session()
    writing_repo = WritingRepository(tmp_path / "agent.db")
    await writing_repo.init()
    service = WritingService(writing_repo)

    task = await service.create_task(session_id, "SQLite 事务")
    task = await service.publish_outline(task.task_id, confirmed_outline(), [citation_snapshot()])

    results = await asyncio.gather(
        service.confirm_outline(
            task.task_id,
            expected_version=task.version,
            outline=confirmed_outline(),
        ),
        service.confirm_outline(
            task.task_id,
            expected_version=task.version,
            outline=confirmed_outline(),
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(result, WritingConflictError) for result in results) == 1
    successful = [result for result in results if not isinstance(result, Exception)]
    assert len(successful) == 1
    assert successful[0].status == WritingStatus.DRAFTING