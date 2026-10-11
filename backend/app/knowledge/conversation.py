from uuid import uuid4


class KnowledgeConversation:
    """Shared RAG turn orchestration for legacy HTTP and leased tasks."""

    def __init__(self, repository, service, llm_client):
        self.repository = repository
        self.service = service
        self.llm_client = llm_client

    async def run(self, session_id, message, *, retrieval_mode="keyword", limit=5, run_id=None):
        history = [
            {"role": event.payload.get("role", "user"), "content": event.payload.get("content", "")}
            for event in await self.repository.list_completed_turn_events(session_id)
            if event.event_type in {"user_message", "assistant_message"}
            and isinstance(event.payload.get("content", ""), str)
        ]
        run_id = run_id or await self.repository.create_run(session_id)
        query = message
        try:
            if history:
                query = await self.service.rewrite_query(message, history, llm_client=self.llm_client)
            await self.repository.append_event(session_id, run_id, "user_message", {
                "role": "user", "content": message, "original_query": message,
                "retrieval_query": query, "retrieval_mode": retrieval_mode})
            kwargs = {"mode": retrieval_mode, "limit": limit, "llm_client": self.llm_client}
            if query != message:
                kwargs["retrieval_query"] = query
            result = await self.service.answer(message, **kwargs)
        except ValueError as exc:
            await self.repository.update_run(run_id, "failed", {"error": str(exc)}, 0)
            raise
        payload = result.model_dump(mode="json")
        await self.repository.append_event(session_id, run_id, "assistant_message",
            {"role": "assistant", "content": payload["answer"], **payload})
        await self.repository.update_run(run_id, "completed",
            {"answer": payload["answer"], "citations": payload["citations"]}, 0)
        return {"run_id": str(run_id), "session_id": str(session_id), "status": "completed",
                "trace_id": str(uuid4()), **payload}