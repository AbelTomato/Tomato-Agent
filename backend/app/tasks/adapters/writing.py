from uuid import UUID

from app.runs.models import PendingAction, RunRepositoryConflict
from app.writing.models import WritingStatus


class WritingAdapter:
    def __init__(self, service, executor, execution):
        self.service = service
        self.executor = executor
        self.execution = execution

    def outcome(self, task, *, unknown_save=False):
        result = task.model_dump(mode="json")
        if task.status == WritingStatus.SAVED and not unknown_save:
            return {"status": "completed", "result": result}
        if task.status == WritingStatus.FAILED:
            return {"status": "failed", "result": result, "error": {"code": task.failed_stage or "writing_failed"}}
        if unknown_save:
            reason, allowed = "save_outcome_unknown", ["cancel", "inspect_recovery"]
        elif task.status == WritingStatus.AWAITING_OUTLINE_CONFIRMATION:
            reason, allowed = "confirm_outline", ["cancel", "confirm_outline"]
        elif task.status == WritingStatus.AWAITING_SAVE_CONFIRMATION:
            reason, allowed = "save_draft", ["cancel", "save_draft"]
        else:
            return {"status": "failed", "error": {"code": "interrupted"}}
        pending = PendingAction(action_id=f"{task.task_id}:{task.version}:{reason}", reason=reason,
            allowed_actions=allowed, business_version=task.version, display_data=result)
        return {"status": "waiting", "result": result, "pending_action": pending}

    async def execute(self, run, lease, port):
        snapshot = await self.execution.get_snapshot(run.id)
        if snapshot.domain_run_id:
            task = await self.service.get_task(UUID(snapshot.domain_run_id))
        else:
            task = await self.service.create_task(run.session_id, run.request["message"])
            snapshot = snapshot.model_copy(update={"domain_run_id": str(task.task_id),
                                                   "input_version": task.version})
        snapshot = snapshot.model_copy(update={"phase": "generating"})
        await self.execution.link_stage(lease, snapshot)
        unknown_save = False
        if snapshot.stage == "confirm_outline":
            if task.status == WritingStatus.AWAITING_OUTLINE_CONFIRMATION:
                task = await self.service.confirm_outline(task.task_id,
                    expected_version=snapshot.pending_action.business_version,
                    outline=snapshot.action_payload["outline"])
            elif task.status != WritingStatus.DRAFTING:
                raise RunRepositoryConflict("domain_version_conflict")
            snapshot = snapshot.model_copy(update={"stage": "drafting", "input_version": task.version})
            await self.execution.link_stage(lease, snapshot)
        if snapshot.stage == "research" and task.status == WritingStatus.RESEARCHING:
            task = await self.executor.execute_research(task.task_id, expected_version=task.version)
        elif snapshot.stage == "drafting" and task.status == WritingStatus.DRAFTING:
            task = await self.executor.execute_draft(task.task_id, expected_version=task.version)
        elif snapshot.stage == "saving":
            try:
                task = await self.service.save(task.task_id,
                    expected_version=snapshot.pending_action.business_version,
                    idempotency_key=snapshot.save_key)
            except Exception:
                # A failed write response is not proof that the side effect failed.
                task = await self.service.get_task(task.task_id)
                known = (task.status == WritingStatus.SAVED and
                         await self.service.repository.has_save_key(task.task_id, snapshot.save_key))
                unknown_save = not known
        values = self.outcome(task, unknown_save=unknown_save)
        pending = values.get("pending_action")
        snapshot = snapshot.model_copy(update={"phase": "waiting" if pending else "finished",
                                               "pending_action": pending})
        await port.finish(**values, snapshot=snapshot, domain_run_id=str(task.task_id),
                          stage=snapshot.stage, event_type="task.result",
                          event_payload={"schema_version": 1, "domain_run_id": str(task.task_id)})

    async def observe_recovery(self, run, snapshot):
        if not snapshot.domain_run_id:
            return {"status": "failed", "error": {"code": "interrupted"}}
        task = await self.service.get_task(UUID(snapshot.domain_run_id))
        if task.status == WritingStatus.SAVED and snapshot.stage == "saving":
            if not await self.service.repository.has_save_key(task.task_id, snapshot.save_key):
                return self.outcome(task, unknown_save=True)
        return self.outcome(task, unknown_save=snapshot.stage == "saving" and task.status != WritingStatus.SAVED)

    async def act(self, run, request):
        if request.action == "inspect_recovery":
            if (run.version != request.expected_version or not run.pending_action or
                    run.pending_action.action_id != request.action_id or
                    request.action not in run.pending_action.allowed_actions):
                raise RunRepositoryConflict("stale_action")
            return run
        replay = await self.execution.get_stage_action_replay(run.id, request)
        if replay is not None:
            return replay
        if request.action not in {"confirm_outline", "save_draft"}:
            raise RunRepositoryConflict("action_not_allowed")
        if run.status == "waiting" and (not run.pending_action or
                request.action not in run.pending_action.allowed_actions):
            raise RunRepositoryConflict("action_not_allowed")
        if request.action == "confirm_outline":
            from app.writing.outline import validate_outline_payload
            task = await self.service.get_task(UUID(run.domain_run_id))
            if set(request.payload) != {"outline"}:
                raise ValueError("outline payload required")
            validate_outline_payload(request.payload["outline"], task.citations)
        elif request.payload:
            raise ValueError("save payload must be empty")
        return await self.execution.queue_stage_action(run.id, request)