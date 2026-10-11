from uuid import UUID

from app.agent.models import CodeTaskRequest
from app.execution.models import StageExecutionSnapshot
from app.execution.ports import canonical_digest
from app.runs.models import RunRepositoryConflict


class TaskService:
    def __init__(self, runs, execution, sessions, adapters, *, enabled, config_digest, model_available=True):
        self.runs = runs
        self.execution = execution
        self.sessions = sessions
        self.adapters = adapters
        self.enabled = enabled
        self.config_digest = config_digest
        self.model_available = model_available

    async def submit(self, session_id, request):
        if not self.enabled:
            raise RuntimeError("worker_disabled")
        if not self.model_available:
            raise RuntimeError("model_unconfigured")
        session_id = UUID(str(session_id))
        if not await self.sessions.session_exists(session_id):
            raise KeyError("session_not_found")
        if request.kind == "code":
            return await self.adapters["code"].service.create_for_session(
                session_id, CodeTaskRequest(task=request.message), request.workspace_id,
                request.client_request_id)
        data = {"message": request.message, "options": request.options}
        run = await self.runs.create_session_run(session_id, request.kind, data, None,
                                                request.client_request_id)
        snapshot = StageExecutionSnapshot(run_id=run.id, kind=request.kind,
            checkpoint_revision=1, last_event_sequence=0, phase="ready", stage="research" if
            request.kind == "writing" else "generation", input_version=0,
            policy_digest=canonical_digest({"kind": request.kind, "protocol": 1}),
            config_digest=self.config_digest)
        await self.execution.bootstrap_snapshot(snapshot)
        return run

    async def execute(self, run, lease, port):
        return await self.adapters[run.task_type if run.task_type != "code_task" else "code"].execute(
            run, lease, port)

    async def recover_stage(self, candidate):
        run = await self.runs.get_run(candidate.run_id)
        snapshot = await self.execution.get_snapshot(run.id)
        if snapshot.config_digest != self.config_digest:
            values = {"status": "failed", "error": {"code": "config_changed"}}
        else:
            values = await self.adapters[run.task_type].observe_recovery(run, snapshot)
        result = await self.execution.recover_stage(candidate, **values)
        return result, (values.get("error") or {}).get("code", "domain_reconciled")

    async def act(self, run_id, request):
        run = await self.runs.get_run(run_id)
        if run is None:
            raise KeyError("run_not_found")
        if request.action != "cancel" and run.task_type == "writing":
            return await self.adapters["writing"].act(run, request)
        if run.version != request.expected_version:
            raise RunRepositoryConflict("version_conflict")
        if request.action == "cancel":
            await self.execution.request_cancel(run.id, expected_version=request.expected_version)
            if run.task_type in {"code", "code_task"}:
                return await self.adapters["code"].service.cancel(str(run.id))
            return await self.runs.get_run(run.id)
        if run.task_type in {"code", "code_task"} and request.action in {"inspect_recovery", "continue_recovery"}:
            if request.action == "continue_recovery" and request.payload.get("confirmed") is not True:
                raise RunRepositoryConflict("confirmation_required")
            return await self.adapters["code"].service.recover(str(run.id),
                expected_version=request.expected_version,
                action="inspect" if request.action == "inspect_recovery" else "continue")
        raise RunRepositoryConflict("action_not_allowed")