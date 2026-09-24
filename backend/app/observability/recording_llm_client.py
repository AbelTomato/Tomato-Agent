from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from copy import deepcopy
from hashlib import sha256
from threading import Lock
from time import perf_counter
from typing import Any, Iterator, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel

from app.agent.interfaces import LLMClient
from app.agent.models import LLMResponse, Message, ToolDefinition
from app.observability.rag_trace import PromptIdentity, canonical_json_sha256

LLMPurpose = Literal["query_planner", "answerer"]


class LLMObservationError(RuntimeError):
    """Raised when an observed LLM call cannot be safely correlated or recorded."""


class LLMCallObserver(Protocol):
    def emit(
        self,
        event_type: str,
        *,
        status: Literal["success", "skipped", "failed"],
        payload: dict[str, Any],
        duration_ms: float | None,
    ) -> None:
        ...


class _PromptScope:
    def __init__(
        self,
        purpose: LLMPurpose,
        prompt: PromptIdentity,
        metadata: dict[str, Any],
    ) -> None:
        self.purpose = purpose
        self.prompt = prompt
        self.metadata = metadata


_question_id_var: ContextVar[str | None] = ContextVar("rag_observe_question_id", default=None)
_prompt_scope_var: ContextVar[_PromptScope | None] = ContextVar("rag_observe_prompt_scope", default=None)
_last_call_var: ContextVar[dict[str, Any] | None] = ContextVar("rag_observe_last_call", default=None)


@contextmanager
def llm_question_scope(question_id: str) -> Iterator[None]:
    """Associate all observed calls in this context with one dataset question."""
    if not isinstance(question_id, str) or not question_id.strip():
        raise ValueError("question_id must be a non-empty string")
    token: Token[str | None] = _question_id_var.set(question_id)
    call_token: Token[dict[str, Any] | None] = _last_call_var.set(None)
    try:
        yield
    finally:
        _last_call_var.reset(call_token)
        _question_id_var.reset(token)


@contextmanager
def llm_prompt_scope(
    purpose: LLMPurpose,
    prompt: PromptIdentity,
    *,
    metadata: dict[str, Any] | None = None,
) -> Iterator[None]:
    """Bind a stable prompt identity and optional audited context to one call."""
    if purpose not in {"query_planner", "answerer"}:
        raise ValueError("unsupported LLM purpose")
    token: Token[_PromptScope | None] = _prompt_scope_var.set(
        _PromptScope(purpose, prompt, deepcopy(metadata or {}))
    )
    try:
        yield
    finally:
        _prompt_scope_var.reset(token)


def make_prompt_identity(prompt_id: str, version: str, template_source: str) -> PromptIdentity:
    """Create prompt metadata whose digest changes with the exact UTF-8 template."""
    if not isinstance(template_source, str):
        raise TypeError("template_source must be a string")
    return PromptIdentity(
        prompt_id=prompt_id,
        version=version,
        sha256=sha256(template_source.encode("utf-8")).hexdigest(),
    )


def _json_model(value: BaseModel) -> dict[str, Any]:
    dumped = value.model_dump(mode="json")
    if not isinstance(dumped, dict):
        raise LLMObservationError("LLM interface model did not serialize to an object")
    return dumped


def _error_code(error: Exception) -> str:
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ValueError):
        return "value_error"
    return "client_error"


class RecordingLLMClient:
    """Transparent optional observer around the project's LLMClient interface.

    Messages and tools are serialized before the call for the audit snapshot but
    the original objects are forwarded unchanged. The captured boundary is the
    Python LLMClient interface, not a provider's HTTP request payload.
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        observer: LLMCallObserver | None = None,
        run_id: UUID | None = None,
        model_id: str | None = None,
        max_calls: int | None = None,
    ) -> None:
        if observer is not None:
            if not isinstance(run_id, UUID):
                raise ValueError("run_id is required when LLM observation is enabled")
            if not isinstance(model_id, str) or not model_id.strip():
                raise ValueError("model_id is required when LLM observation is enabled")
        self.client = client
        self.observer = observer
        self.run_id = run_id
        self.model_id = model_id
        if max_calls is not None and max_calls <= 0:
            raise ValueError("max_calls must be greater than zero")
        self.max_calls = max_calls
        self._call_count = 0
        self._attempts: dict[tuple[str, LLMPurpose], int] = {}
        self._attempt_lock = Lock()

    def _reserve_call(self) -> None:
        with self._attempt_lock:
            if self.max_calls is not None and self._call_count >= self.max_calls:
                raise LLMObservationError("LLM client call limit reached")
            self._call_count += 1

    def _next_attempt(self, question_id: str, purpose: LLMPurpose) -> int:
        key = (question_id, purpose)
        with self._attempt_lock:
            attempt = self._attempts.get(key, 0) + 1
            self._attempts[key] = attempt
            return attempt

    def emit_observation(
        self,
        event_type: str,
        *,
        status: Literal["success", "skipped", "failed"],
        payload: dict[str, Any],
        duration_ms: float | None = None,
    ) -> None:
        """Emit a service-level follow-up event through this client's observer."""
        if self.observer is None:
            return
        question_id = _question_id_var.get()
        if question_id is None:
            raise LLMObservationError("LLM observation requires an active question scope")
        self.observer.emit(
            event_type,
            status=status,
            payload={**deepcopy(payload), **deepcopy(_last_call_var.get() or {})},
            duration_ms=duration_ms,
        )

    def emit_skipped(
        self,
        purpose: LLMPurpose,
        prompt: PromptIdentity,
        *,
        reason: str,
    ) -> None:
        if self.observer is None:
            return
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("skip reason must be a non-empty stable classification")
        question_id = _question_id_var.get()
        if question_id is None:
            raise LLMObservationError("LLM observation requires an active question scope")
        self.observer.emit(
            "llm.skipped",
            status="skipped",
            payload={
                "question_id": question_id,
                "purpose": purpose,
                "model_id": self.model_id,
                "prompt": prompt.model_dump(mode="json"),
                "reason": reason,
            },
            duration_ms=None,
        )

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
    ) -> LLMResponse:
        self._reserve_call()
        if self.observer is None:
            return await self.client.complete(messages, tools)

        question_id = _question_id_var.get()
        scope = _prompt_scope_var.get()
        if question_id is None:
            raise LLMObservationError("LLM observation requires an active question scope")
        if scope is None:
            raise LLMObservationError("LLM observation requires an active prompt scope")

        # Freeze the precise interface values before downstream code can mutate them.
        message_snapshot = deepcopy([_json_model(message) for message in messages])
        tools_snapshot = deepcopy([_json_model(tool) for tool in tools])
        messages_sha256 = canonical_json_sha256(message_snapshot)
        tools_sha256 = canonical_json_sha256(tools_snapshot)
        attempt = self._next_attempt(question_id, scope.purpose)
        call_id = str(uuid4())
        prompt_payload = scope.prompt.model_dump(mode="json")
        request_payload = {
            "call_id": call_id,
            "question_id": question_id,
            "purpose": scope.purpose,
            "attempt": attempt,
            "model_id": self.model_id,
            "prompt": prompt_payload,
            "messages": message_snapshot,
            "tools": tools_snapshot,
            "messages_sha256": messages_sha256,
            "tools_sha256": tools_sha256,
            **deepcopy(scope.metadata),
        }
        try:
            self.observer.emit(
                "llm.request",
                status="success",
                payload=request_payload,
                duration_ms=None,
            )
        except Exception as exc:
            raise LLMObservationError("unable to record LLM request event") from exc

        started = perf_counter()
        try:
            response = await self.client.complete(messages, tools)
        except Exception as exc:
            duration_ms = (perf_counter() - started) * 1000
            _last_call_var.set(
                {
                    "llm_call_id": call_id,
                    "llm_purpose": scope.purpose,
                    "llm_attempt": attempt,
                    "messages_sha256": messages_sha256,
                    "tools_sha256": tools_sha256,
                }
            )
            try:
                self.observer.emit(
                    "llm.response",
                    status="failed",
                    payload={
                        "call_id": call_id,
                        "question_id": question_id,
                        "purpose": scope.purpose,
                        "attempt": attempt,
                        "model_id": self.model_id,
                        "messages_sha256": messages_sha256,
                        "tools_sha256": tools_sha256,
                        "status": "failed",
                        "error_code": _error_code(exc),
                    },
                    duration_ms=duration_ms,
                )
            except Exception:
                # Never replace the provider/client exception with an audit error.
                pass
            raise

        duration_ms = (perf_counter() - started) * 1000
        _last_call_var.set(
            {
                "llm_call_id": call_id,
                "llm_purpose": scope.purpose,
                "llm_attempt": attempt,
                "messages_sha256": messages_sha256,
                "tools_sha256": tools_sha256,
            }
        )
        try:
            response_payload = _json_model(response)
        except Exception as exc:
            try:
                self.observer.emit(
                    "llm.response",
                    status="failed",
                    payload={
                        "call_id": call_id,
                        "question_id": question_id,
                        "purpose": scope.purpose,
                        "attempt": attempt,
                        "model_id": self.model_id,
                        "messages_sha256": messages_sha256,
                        "tools_sha256": tools_sha256,
                        "status": "failed",
                        "error_code": "invalid_response_object",
                    },
                    duration_ms=duration_ms,
                )
            except Exception:
                pass
            raise LLMObservationError("LLM response is not serializable for the trace") from exc

        self.observer.emit(
            "llm.response",
            status="success",
            payload={
                "call_id": call_id,
                "question_id": question_id,
                "purpose": scope.purpose,
                "attempt": attempt,
                "model_id": self.model_id,
                "messages_sha256": messages_sha256,
                "tools_sha256": tools_sha256,
                "status": "success",
                "response": response_payload,
            },
            duration_ms=duration_ms,
        )
        return response