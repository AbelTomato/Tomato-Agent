"""Hard-exit after a durable domain effect, before the outer Run commit."""

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

from app.execution.repository import ExecutionRepository
from app.sessions.repository import SessionRepository
from app.writing.repository import WritingRepository
from app.writing.service import WritingService
from app.execution.models import OperationInput
from uuid import uuid4


async def main():
    path, run_id, scenario, drafts = sys.argv[1:]
    repository = ExecutionRepository(Path(path))
    ident = UUID(run_id)
    lease = await repository.claim(ident, "crash-child", lease_seconds=1)
    snapshot = await repository.get_snapshot(ident)
    if scenario == "code_write":
        operation = OperationInput(id=uuid4(), logical_index=0, kind="tool", tool_name="write_file",
            input_payload={"path": "app.py"}, recovery_class="file_write")
        snapshot = snapshot.model_copy(update={"pending_operation_id": operation.id})
        await repository.prepare_operation(lease, operation, snapshot)
        await repository.start_step(lease, operation.id, snapshot)
        Path(drafts).write_text("value = 99\n")
        os._exit(73)
    snapshot = snapshot.model_copy(update={"phase": "generating"})
    if scenario == "generating":
        sessions = SessionRepository(Path(path))
        run = await repository.get_run(ident)
        domain_id = await sessions.create_run(run.session_id)
        snapshot = snapshot.model_copy(update={"domain_run_id": str(domain_id)})
    await repository.link_stage(lease, snapshot)
    service = WritingService(WritingRepository(Path(path)), draft_directory=Path(drafts))
    if scenario == "outline":
        task = await service.get_task(UUID(snapshot.domain_run_id))
        await service.confirm_outline(task.task_id, expected_version=task.version,
                                      outline=snapshot.action_payload["outline"])
    elif scenario == "saved":
        await service.save(UUID(snapshot.domain_run_id),
            expected_version=snapshot.pending_action.business_version, idempotency_key=snapshot.save_key)
    elif scenario == "unknown_save":
        # Partial filesystem effect without the domain save key is deliberately unconfirmed.
        Path(drafts).mkdir(parents=True, exist_ok=True)
        (Path(drafts) / "unknown.md").write_text("unconfirmed partial write\n")
    os._exit(73)


asyncio.run(main())