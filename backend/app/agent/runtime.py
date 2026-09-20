import json
from dataclasses import asdict, dataclass
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from app.sessions.repository import SessionRepository
from app.tools.base import ToolContext, ToolResult
from app.tools.registry import ToolRegistry

from .config import RuntimeConfig
from .context import ContextManager
from .interfaces import LLMClient
from .models import ContextState, LLMResponse, Message, RunResult


@dataclass
class RuntimeCounters:
    """可持久化的 Runtime 执行计数。"""

    loop_count: int = 0
    tool_call_count: int = 0
    elapsed_seconds: float = 0.0
    last_sequence: int = 0


class AgentRuntime:
    def __init__(
        self,
        llm: LLMClient,
        context_manager: ContextManager,
        tool_registry: ToolRegistry,
        repository: SessionRepository,
        system_instruction: str,
        config: RuntimeConfig | None = None,
    ):
        self.llm = llm
        self.context_manager = context_manager
        self.tool_registry = tool_registry
        self.repository = repository
        self.system_instruction = system_instruction
        self.config = config or RuntimeConfig()

    async def _start_or_resume_run(
        self,
        session_id: UUID,
        trace_id: UUID,
        run_id: UUID | None = None,
    ) -> UUID:
        if run_id is None:
            run_id = await self.repository.create_run(session_id)
            await self.repository.append_event(
                session_id,
                run_id,
                "run_started",
                {"trace_id": str(trace_id)},
            )
        else:
            existing = await self.repository.get_run(run_id, session_id)
            if existing is None:
                raise ValueError(f"Run not found: {run_id}")
            if existing.status in {"completed", "failed"}:
                raise ValueError(
                    f"Run {run_id} is terminal and cannot be resumed"
                )

        return run_id

    async def _load_runtime_state(
        self,
        session_id: UUID,
        run_id: UUID,
        *,
        include_previous_history: bool = False,
    ) -> tuple[list[Message], ContextState, RuntimeCounters]:
        run = await self.repository.get_run(run_id, session_id)
        if run is None:
            raise ValueError(f"Run not found: {run_id}")

        session = await self.repository.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")

        checkpoint = await self.repository.get_checkpoint(run_id)
        state, counters = self._restore_checkpoint(checkpoint.state if checkpoint else None)
        if checkpoint is not None:
            counters.last_sequence = checkpoint.sequence

        if checkpoint is None:
            metadata_state = {
                key: session.metadata[key]
                for key in ContextState.model_fields
                if key in session.metadata
            }
            state = ContextState.model_validate(metadata_state)

        messages: list[Message] = []
        if include_previous_history:
            previous_events = await self.repository.list_completed_turn_events(session_id)
            for event in previous_events:
                message = self._message_from_event(event.event_type, event.payload)
                if message is not None:
                    messages.append(message)

        events = await self.repository.list_events(run_id)
        for event in events:
            message = self._message_from_event(event.event_type, event.payload)
            if message is not None:
                messages.append(message)
            counters.last_sequence = max(counters.last_sequence, event.sequence)

        return messages, state, counters

    def _restore_checkpoint(
        self,
        raw_state: dict[str, Any] | None,
    ) -> tuple[ContextState, RuntimeCounters]:
        if not raw_state:
            return ContextState(), RuntimeCounters()

        context_data = raw_state.get("context", raw_state)
        counter_data = raw_state.get("counters", {})
        state = ContextState.model_validate(context_data)
        counters = RuntimeCounters(
            loop_count=max(0, int(counter_data.get("loop_count", 0))),
            tool_call_count=max(0, int(counter_data.get("tool_call_count", 0))),
            elapsed_seconds=max(0.0, float(counter_data.get("elapsed_seconds", 0.0))),
            last_sequence=max(0, int(counter_data.get("last_sequence", 0))),
        )
        return state, counters

    def _message_from_event(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> Message | None:
        if event_type == "user_message":
            return Message(role="user", content=str(payload.get("content", "")))
        if event_type == "assistant_message":
            return Message(
                role="assistant",
                content=str(payload.get("content", "")),
                tool_calls=payload.get("tool_calls", []),
            )
        if event_type == "tool_result":
            return Message(
                role="tool",
                content=str(payload.get("content", "")),
                tool_call_id=payload.get("tool_call_id"),
            )
        return None

    async def _append_user_message(
        self,
        session_id: UUID,
        run_id: UUID,
        message: str,
        trace_id: UUID,
    ) -> int:
        return await self.repository.append_event(
            session_id,
            run_id,
            "user_message",
            {"content": message, "trace_id": str(trace_id)},
        )

    def _check_budget(
        self,
        loop_count: int,
        tool_call_count: int,
        started_at: float,
        persisted_elapsed: float,
    ) -> None:
        elapsed = max(monotonic() - started_at, persisted_elapsed)
        if loop_count >= self.config.max_loops:
            raise RuntimeError("Maximum loop count exceeded")
        if tool_call_count >= self.config.max_tool_calls:
            raise RuntimeError("Maximum tool call count exceeded")
        if elapsed >= self.config.max_duration_seconds:
            raise TimeoutError("Maximum runtime duration exceeded")

    def _validate_response(self, response: LLMResponse) -> None:
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

    async def _execute_tool_call(
        self,
        session_id: UUID,
        run_id: UUID,
        messages: list[Message],
        state: ContextState,
        response: LLMResponse,
        counters: RuntimeCounters,
        trace_id: UUID,
    ) -> list[Message]:
        if response.tool_call is None:
            raise ValueError("Tool call response is missing tool_call")

        tool_call = response.tool_call
        assistant_message = Message(role="assistant", tool_calls=[tool_call])
        messages.append(assistant_message)
        await self.repository.append_event(
            session_id,
            run_id,
            "assistant_message",
            {
                "content": assistant_message.content,
                "tool_calls": [tool_call.model_dump(mode="json")],
                "trace_id": str(trace_id),
            },
        )

        counters.tool_call_count += 1
        try:
            result = await self.tool_registry.execute(
                tool_call.name,
                tool_call.arguments,
                ToolContext(session_id=str(session_id), run_id=str(run_id)),
                timeout=self.config.tool_timeout_seconds,
            )
        except Exception as exc:
            result = ToolResult(success=False, error=str(exc))

        content = self._serialize_tool_result(result)
        messages.append(
            Message(
                role="tool",
                content=content,
                tool_call_id=tool_call.call_id,
            )
        )
        sequence = await self.repository.append_event(
            session_id,
            run_id,
            "tool_result",
            {
                "tool_call_id": tool_call.call_id,
                "tool_name": tool_call.name,
                "content": content,
                "success": result.success,
                "trace_id": str(trace_id),
            },
        )
        counters.last_sequence = sequence
        await self._save_runtime_state(run_id, sequence, state, counters)
        return messages

    def _serialize_tool_result(self, result: ToolResult) -> str:
        return json.dumps(
            {
                "success": result.success,
                "data": result.data,
                "error": result.error,
            },
            ensure_ascii=False,
            default=str,
        )

    async def _complete_run(
        self,
        session_id: UUID,
        run_id: UUID,
        response: LLMResponse,
        state: ContextState,
        counters: RuntimeCounters,
        trace_id: UUID,
    ) -> RunResult:
        sequence = await self.repository.append_event(
            session_id,
            run_id,
            "assistant_message",
            {"content": response.content or "", "trace_id": str(trace_id)},
        )
        counters.last_sequence = sequence
        terminal_sequence = await self.repository.append_event(
            session_id,
            run_id,
            "run_completed",
            {"trace_id": str(trace_id)},
        )
        counters.last_sequence = terminal_sequence
        await self.repository.update_run(
            run_id,
            "completed",
            self._checkpoint_state(state, counters),
            loop_count=counters.loop_count,
        )
        await self._save_runtime_state(run_id, terminal_sequence, state, counters)
        return RunResult(
            run_id=run_id,
            session_id=session_id,
            status="completed",
            answer=response.content,
            trace_id=trace_id,
            loop_count=counters.loop_count,
        )

    async def _pause_for_clarification(
        self,
        session_id: UUID,
        run_id: UUID,
        response: LLMResponse,
        state: ContextState,
        counters: RuntimeCounters,
        trace_id: UUID,
    ) -> RunResult:
        sequence = await self.repository.append_event(
            session_id,
            run_id,
            "assistant_message",
            {"content": response.content or "", "trace_id": str(trace_id)},
        )
        counters.last_sequence = sequence
        terminal_sequence = await self.repository.append_event(
            session_id,
            run_id,
            "run_paused",
            {"reason": "clarification_required", "trace_id": str(trace_id)},
        )
        counters.last_sequence = terminal_sequence
        await self.repository.update_run(
            run_id,
            "paused",
            self._checkpoint_state(state, counters),
            loop_count=counters.loop_count,
        )
        await self._save_runtime_state(run_id, terminal_sequence, state, counters)
        return RunResult(
            run_id=run_id,
            session_id=session_id,
            status="paused",
            answer=response.content,
            trace_id=trace_id,
            loop_count=counters.loop_count,
        )

    async def _fail_run(
        self,
        session_id: UUID,
        run_id: UUID,
        state: ContextState,
        counters: RuntimeCounters,
        trace_id: UUID,
        exc: Exception,
    ) -> RunResult:
        error = {"type": type(exc).__name__, "message": str(exc)}
        sequence = await self.repository.append_event(
            session_id,
            run_id,
            "run_failed",
            {"error": error, "trace_id": str(trace_id)},
        )
        counters.last_sequence = sequence
        await self.repository.update_run(
            run_id,
            "failed",
            self._checkpoint_state(state, counters),
            loop_count=counters.loop_count,
        )
        await self._save_runtime_state(run_id, sequence, state, counters)
        return RunResult(
            run_id=run_id,
            session_id=session_id,
            status="failed",
            trace_id=trace_id,
            loop_count=counters.loop_count,
            error=error,
        )

    def _checkpoint_state(
        self,
        state: ContextState,
        counters: RuntimeCounters,
    ) -> dict[str, Any]:
        return {"context": state.model_dump(mode="json"), "counters": asdict(counters)}

    async def _save_runtime_state(
        self,
        run_id: UUID,
        sequence: int,
        state: ContextState,
        counters: RuntimeCounters,
    ) -> None:
        await self.repository.save_checkpoint(
            run_id,
            sequence,
            self._checkpoint_state(state, counters),
        )

    async def run(
        self,
        session_id: UUID,
        user_message: str | None = None,
        run_id: UUID | None = None,
    ) -> RunResult:
        trace_id = uuid4()
        started_at = monotonic()
        is_new_run = run_id is None
        run_id = await self._start_or_resume_run(
            session_id=session_id,
            run_id=run_id,
            trace_id=trace_id,
        )
        messages, state, counters = await self._load_runtime_state(
            session_id=session_id,
            run_id=run_id,
            include_previous_history=is_new_run,
        )

        if user_message is not None:
            messages.append(Message(role="user", content=user_message))
            counters.last_sequence = await self._append_user_message(
                session_id=session_id,
                run_id=run_id,
                message=user_message,
                trace_id=trace_id,
            )

        try:
            while True:
                self._check_budget(
                    loop_count=counters.loop_count,
                    tool_call_count=counters.tool_call_count,
                    started_at=started_at,
                    persisted_elapsed=counters.elapsed_seconds,
                )
                counters.loop_count += 1
                state = self.context_manager.compact(state, messages)
                context_messages = self.context_manager.build(
                    self.system_instruction,
                    state,
                    messages,
                )
                response = await self.llm.complete(
                    context_messages,
                    self.tool_registry.definitions(),
                )
                self._validate_response(response)

                if response.kind == "final":
                    return await self._complete_run(
                        session_id,
                        run_id,
                        response,
                        state,
                        counters,
                        trace_id,
                    )
                if response.kind == "clarification":
                    return await self._pause_for_clarification(
                        session_id,
                        run_id,
                        response,
                        state,
                        counters,
                        trace_id,
                    )
                messages = await self._execute_tool_call(
                    session_id,
                    run_id,
                    messages,
                    state,
                    response,
                    counters,
                    trace_id,
                )
                counters.elapsed_seconds = max(
                    counters.elapsed_seconds,
                    monotonic() - started_at,
                )
        except Exception as exc:
            counters.elapsed_seconds = max(
                counters.elapsed_seconds,
                monotonic() - started_at,
            )
            return await self._fail_run(
                session_id,
                run_id,
                state,
                counters,
                trace_id,
                exc,
            )