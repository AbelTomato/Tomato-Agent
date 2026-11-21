"""Bounded, thread-safe in-memory storage for completed Runtime spans."""

from collections import deque
from threading import Lock
from typing import Any
from uuid import UUID, uuid4

from app.observability.trace_models import RuntimeTracePage, RuntimeTraceSpan


DEFAULT_TRACE_BUFFER_CAPACITY = 4096
MAX_TRACE_PAGE_SIZE = 500


class RuntimeTraceBuffer:
    def __init__(self, *, capacity: int = DEFAULT_TRACE_BUFFER_CAPACITY,
                 runtime_instance_id: UUID | None = None) -> None:
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("trace buffer capacity must be a positive integer")
        self.runtime_instance_id = runtime_instance_id or uuid4()
        self._capacity = capacity
        self._items: deque[RuntimeTraceSpan] = deque(maxlen=capacity)
        self._lock = Lock()
        self._next_sequence = 1
        self._dropped_total = 0

    def append(self, **fields: Any) -> RuntimeTraceSpan:
        with self._lock:
            sequence = self._next_sequence
            span = RuntimeTraceSpan(
                schema_version="runtime-trace/v1",
                event_id=uuid4(),
                runtime_instance_id=self.runtime_instance_id,
                sequence=sequence,
                **fields,
            )
            if len(self._items) == self._capacity:
                self._dropped_total += 1
            self._items.append(span)
            self._next_sequence += 1
            return span

    def read_after(self, after: int, *, limit: int) -> RuntimeTracePage:
        if type(after) is not int or after < 0:
            raise ValueError("trace cursor must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= MAX_TRACE_PAGE_SIZE:
            raise ValueError(f"trace page limit must be between 1 and {MAX_TRACE_PAGE_SIZE}")

        with self._lock:
            oldest_sequence = self._items[0].sequence if self._items else None
            gap_count = (
                max(0, oldest_sequence - after - 1)
                if oldest_sequence is not None
                else 0
            )
            items = tuple(item for item in self._items if item.sequence > after)[:limit]
            next_cursor = items[-1].sequence if items else after
            return RuntimeTracePage(
                schema_version="runtime-trace/v1",
                items=items,
                next_cursor=next_cursor,
                dropped_total=self._dropped_total,
                oldest_sequence=oldest_sequence,
                gap_count=gap_count,
                runtime_instance_id=self.runtime_instance_id,
            )
