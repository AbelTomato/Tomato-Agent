import logging
from contextlib import contextmanager
from time import perf_counter
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


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
