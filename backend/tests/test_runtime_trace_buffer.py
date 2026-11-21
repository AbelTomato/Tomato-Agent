from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from importlib.util import find_spec
from uuid import UUID

import pytest


def make_buffer(capacity=4096):
    from app.observability.trace_buffer import RuntimeTraceBuffer

    return RuntimeTraceBuffer(capacity=capacity)


def span_fields(**changes):
    fields = {
        "trace_id": UUID("00000000-0000-0000-0000-000000000001"),
        "span_id": UUID("00000000-0000-0000-0000-000000000002"),
        "parent_span_id": None,
        "run_id": UUID("00000000-0000-0000-0000-000000000003"),
        "operation": "tool.execute",
        "started_at": datetime(2026, 11, 20, tzinfo=timezone.utc),
        "duration_ms": 1.0,
        "status": "ok",
        "error_code": None,
        "error_category": None,
        "attributes": {"tool_name": "read_file"},
    }
    fields.update(changes)
    return fields


def test_trace_buffer_module_exposes_cursor_reads():
    assert find_spec("app.observability.trace_buffer") is not None
    buffer = make_buffer(capacity=3)
    first = buffer.append(**span_fields())
    second = buffer.append(**span_fields(span_id=UUID("00000000-0000-0000-0000-000000000004")))

    page = buffer.read_after(first.sequence, limit=10)

    assert [item.event_id for item in page.items] == [second.event_id]
    assert page.next_cursor == second.sequence
    assert page.runtime_instance_id == first.runtime_instance_id


def test_trace_buffer_overwrites_oldest_and_reports_gap_and_drop_count():
    buffer = make_buffer(capacity=2)
    spans = [buffer.append(**span_fields()) for _ in range(3)]

    page = buffer.read_after(0, limit=10)

    assert [item.sequence for item in page.items] == [2, 3]
    assert page.dropped_total == 1
    assert page.oldest_sequence == 2
    assert page.gap_count == 1


def test_trace_buffer_assigns_unique_sequences_under_concurrent_writes():
    buffer = make_buffer(capacity=256)

    with ThreadPoolExecutor(max_workers=8) as pool:
        spans = list(pool.map(lambda _: buffer.append(**span_fields()), range(128)))

    assert sorted(item.sequence for item in spans) == list(range(1, 129))
    assert len({item.event_id for item in spans}) == 128
    assert len({item.runtime_instance_id for item in spans}) == 1


@pytest.mark.parametrize("capacity", [0, -1])
def test_trace_buffer_rejects_non_positive_capacity(capacity):
    with pytest.raises(ValueError):
        make_buffer(capacity=capacity)


@pytest.mark.parametrize("after,limit", [(-1, 1), (0, 0), (0, 501)])
def test_trace_buffer_rejects_invalid_cursors_and_page_sizes(after, limit):
    buffer = make_buffer()

    with pytest.raises(ValueError):
        buffer.read_after(after, limit=limit)
