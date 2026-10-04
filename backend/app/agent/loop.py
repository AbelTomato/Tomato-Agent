from dataclasses import dataclass
from time import monotonic

from .config import RuntimeConfig
from .context import ContextManager
from .interfaces import LLMClient
from .models import ContextState, LLMResponse, Message, ToolDefinition


@dataclass(frozen=True)
class AgentLoopResult:
    response: LLMResponse
    state: ContextState
    loop_count: int


class AgentLoop:
    """Stateless-per-turn LLM decision step; it does not access persistence."""

    def __init__(
        self,
        llm: LLMClient,
        context_manager: ContextManager,
        system_instruction: str,
        config: RuntimeConfig,
        tools: list[ToolDefinition],
    ) -> None:
        self.llm = llm
        self.context_manager = context_manager
        self.system_instruction = system_instruction
        self.config = config
        self.tools = tools

    async def next_response(
        self,
        messages: list[Message],
        state: ContextState,
        *,
        loop_count: int,
        tool_call_count: int,
        started_at: float,
        persisted_elapsed: float,
    ) -> AgentLoopResult:
        elapsed = max(monotonic() - started_at, persisted_elapsed)
        if loop_count >= self.config.max_loops:
            raise RuntimeError("Maximum loop count exceeded")
        if tool_call_count >= self.config.max_tool_calls:
            raise RuntimeError("Maximum tool call count exceeded")
        if elapsed >= self.config.max_duration_seconds:
            raise TimeoutError("Maximum runtime duration exceeded")

        next_count = loop_count + 1
        next_state = self.context_manager.compact(state, messages)
        context_messages = self.context_manager.build(
            self.system_instruction,
            next_state,
            messages,
        )
        response = await self.llm.complete(context_messages, self.tools)
        self._validate_response(response)
        return AgentLoopResult(response, next_state, next_count)

    @staticmethod
    def _validate_response(response: LLMResponse) -> None:
        if response.kind in {"final", "clarification"}:
            if not response.content or not response.content.strip():
                raise ValueError(f"LLM {response.kind} response must contain content")
            return
        if response.kind == "tool_call":
            if response.tool_call is None:
                raise ValueError("LLM tool_call response must contain a tool call")
            if not response.tool_call.name:
                raise ValueError("Tool call name cannot be empty")
            if not isinstance(response.tool_call.arguments, dict):
                raise ValueError("Tool call arguments must be an object")
            return
        raise ValueError(f"Unsupported LLM response kind: {response.kind}")