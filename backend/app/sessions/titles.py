import asyncio

from app.agent.models import LLMResponse, Message


class SessionTitleService:
    """Optional one-shot title helper, isolated from business Run accounting."""

    def __init__(self, repository, llm, *, enabled=False, timeout_seconds=30):
        self.repository = repository
        self.llm = llm
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds

    async def generate(self, session_id, user_input, first_result):
        if not self.enabled:
            return None
        session = await self.repository.get_session(session_id)
        if session is None or session.title_source == "manual":
            return None
        title = await self.title_for(user_input, first_result)
        if title:
            await self.repository.set_generated_title(session_id, title, session.title_version)
        return title

    async def title_for(self, user_input, first_result):
        if not self.enabled or self.llm is None:
            return None
        messages = [
            Message(role="system", content="Create a concise single-line conversation title of at most 30 characters. Return only the title."),
            Message(role="user", content=f"Request: {user_input[:2000]}\nResult: {first_result[:2000]}"),
        ]
        try:
            response = await asyncio.wait_for(
                self.llm.complete(messages, []), timeout=self.timeout_seconds
            )
        except (Exception, asyncio.TimeoutError):
            return None
        if not isinstance(response, LLMResponse) or response.kind != "final":
            return None
        title = " ".join((response.content or "").split())[:30].strip(" \"'`#")
        return title or None