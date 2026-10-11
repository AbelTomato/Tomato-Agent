from uuid import UUID


class KnowledgeAdapter:
    def __init__(self, conversation):
        self.conversation = conversation

    async def execute(self, run, lease, port):
        snapshot = await port.repository.get_snapshot(run.id)
        domain_id = snapshot.domain_run_id or str(await self.conversation.repository.create_run(run.session_id))
        snapshot = snapshot.model_copy(update={"domain_run_id": domain_id, "phase": "generating"})
        await port.repository.link_stage(lease, snapshot)
        result = await self.conversation.run(run.session_id, run.request["message"],
            run_id=UUID(domain_id), **run.request["options"])
        await port.finish("completed", snapshot=snapshot.model_copy(update={"phase": "finished"}),
            result=result, domain_run_id=domain_id, stage="generation", event_type="task.result",
            event_payload={"schema_version": 1, "domain_run_id": domain_id})

    async def observe_recovery(self, run, snapshot):
        if snapshot.domain_run_id:
            domain = await self.conversation.repository.get_run(UUID(snapshot.domain_run_id), run.session_id)
            if domain and domain.status == "completed":
                events = await self.conversation.repository.list_events(UUID(snapshot.domain_run_id))
                answers = [event.payload for event in events if event.event_type == "assistant_message"]
                if answers:
                    return {"status": "completed", "result": answers[-1]}
        return {"status": "failed", "error": {"code": "interrupted"}}