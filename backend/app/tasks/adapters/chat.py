from uuid import UUID


class ChatAdapter:
    def __init__(self, runtime, sessions):
        self.runtime = runtime
        self.sessions = sessions

    async def execute(self, run, lease, port):
        snapshot = await port.repository.get_snapshot(run.id)
        # Create and persist the domain ID before any generation starts.
        domain_id = snapshot.domain_run_id
        if domain_id is None:
            domain_id = str(await self.sessions.create_run(run.session_id))
        snapshot = snapshot.model_copy(update={"domain_run_id": domain_id, "phase": "generating"})
        await port.repository.link_stage(lease, snapshot)
        result = await self.runtime.run(run.session_id, run.request["message"], UUID(domain_id),
                                        include_previous_history=True)
        await port.finish("completed" if result.status == "completed" else "failed",
            snapshot=snapshot.model_copy(update={"phase": "finished"}),
            event_type="task.result", event_payload={"schema_version": 1, "domain_run_id": domain_id},
            result=result.model_dump(mode="json"), domain_run_id=domain_id, stage="generation",
            error=result.error if result.status == "failed" else None)

    async def observe_recovery(self, run, snapshot):
        if snapshot.domain_run_id:
            domain = await self.sessions.get_run(UUID(snapshot.domain_run_id), run.session_id)
            if domain and domain.status == "completed":
                events = await self.sessions.list_events(UUID(snapshot.domain_run_id))
                answers = [event.payload for event in events if event.event_type == "assistant_message"]
                if answers:
                    return {"status": "completed", "result": {"answer": answers[-1]["content"],
                        "run_id": snapshot.domain_run_id, "session_id": str(run.session_id),
                        "status": "completed", "loop_count": domain.loop_count,
                        "trace_id": answers[-1].get("trace_id")}}
        return {"status": "failed", "error": {"code": "interrupted"}}