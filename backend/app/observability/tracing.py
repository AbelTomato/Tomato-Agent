import asyncio
import logging
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic, perf_counter
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ActiveSpan:
    buffer: object
    run_id: UUID
    trace_id: UUID
    span_id: UUID


@dataclass
class SpanOutcome:
    status: str = "ok"
    error_code: str | None = None
    error_category: str | None = None

    def fail(self, code: str, category: str) -> None:
        self.status = "error"
        self.error_code = code
        self.error_category = category


_ACTIVE_SPAN: ContextVar[_ActiveSpan | None] = ContextVar("runtime_trace_span", default=None)


def _error_metadata(operation: str, error: BaseException) -> tuple[str, str]:
    if isinstance(error, asyncio.CancelledError):
        return "cancelled", "cancelled"
    if isinstance(error, TimeoutError):
        return "timeout", "timeout"
    category = {
        "run": "runtime",
        "model": "provider",
        "tool": "tool",
        "memory": "memory",
        "knowledge": "knowledge",
        "validation": "validation",
    }.get(operation.partition(".")[0], "unknown")
    return "operation_failed", category


def _append_span(buffer, fields: dict) -> None:
    try:
        buffer.append(**fields)
    except Exception as error:
        logger.warning("runtime trace append failed error_type=%s", type(error).__name__)


@contextmanager
def _trace_span_context(operation: str, *, run_id: UUID | None = None, buffer=None, attributes=None):
    """Internal span context shared by sync and async instrumentation sites."""
    parent = _ACTIVE_SPAN.get()
    active_buffer = buffer if buffer is not None else (parent.buffer if parent else None)
    active_run_id = run_id if run_id is not None else (parent.run_id if parent else None)
    if active_buffer is None or active_run_id is None:
        yield None
        return

    trace_id = parent.trace_id if parent else (active_run_id if operation == "run.execute" else uuid4())
    span_id = uuid4()
    active = _ActiveSpan(active_buffer, active_run_id, trace_id, span_id)
    token = _ACTIVE_SPAN.set(active)
    started_at = datetime.now(timezone.utc)
    started = monotonic()
    outcome = SpanOutcome()
    try:
        yield outcome
    except asyncio.CancelledError as error:
        outcome.status = "cancelled"
        outcome.error_code, outcome.error_category = _error_metadata(operation, error)
        raise
    except TimeoutError as error:
        outcome.status = "timeout"
        outcome.error_code, outcome.error_category = _error_metadata(operation, error)
        raise
    except Exception as error:
        outcome.fail(*_error_metadata(operation, error))
        raise
    finally:
        duration_ms = max(0.0, (monotonic() - started) * 1000)
        _ACTIVE_SPAN.reset(token)
        _append_span(active_buffer, {
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent.span_id if parent else None,
            "run_id": active_run_id,
            "operation": operation,
            "started_at": started_at,
            "duration_ms": duration_ms,
            "status": outcome.status,
            "error_code": outcome.error_code,
            "error_category": outcome.error_category,
            "attributes": attributes or {},
        })


@asynccontextmanager
async def runtime_span(operation: str, *, run_id: UUID | None = None, buffer=None, attributes=None):
    """Record one completed span without persisting request or exception text."""
    with _trace_span_context(operation, run_id=run_id, buffer=buffer, attributes=attributes) as outcome:
        yield outcome


@contextmanager
def span(name: str, trace_id: UUID | None = None):
    trace_id = trace_id or uuid4()
    started = perf_counter()
    try:
        yield trace_id
        logger.info(
            "trace=%s span=%s status=success duration_ms=%.2f",
            trace_id,
            name,
            (perf_counter() - started) * 1000,
        )
    except Exception:
        logger.exception("trace=%s span=%s status=failed", trace_id, name)
        raise
