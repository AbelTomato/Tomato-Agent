import asyncio
from importlib import import_module
from uuid import UUID

import pytest

from app.observability.trace_buffer import RuntimeTraceBuffer
from test_assistant_api import assistant_api, decision, request


RUN_ID = UUID("00000000-0000-0000-0000-000000000005")


def test_runtime_span_api_is_available():
    tracing = import_module("app.observability.tracing")
    assert hasattr(tracing, "runtime_span")


async def test_runtime_spans_link_parent_child_and_classify_failures_without_text():
    from app.observability.tracing import runtime_span

    buffer = RuntimeTraceBuffer()
    async with runtime_span("run.execute", run_id=RUN_ID, buffer=buffer):
        async with runtime_span("model.complete", attributes={"model_name": "test-model"}):
            raise_value = "private provider body"
            with pytest.raises(RuntimeError, match=raise_value):
                async with runtime_span("tool.execute", attributes={"tool_name": "read_file"}):
                    raise RuntimeError(raise_value)

    page = buffer.read_after(0, limit=10)
    spans = {span.operation: span for span in page.items}
    root = spans["run.execute"]
    child = spans["model.complete"]
    grandchild = spans["tool.execute"]

    assert root.operation == "run.execute"
    assert root.parent_span_id is None
    assert root.trace_id == RUN_ID
    assert root.status == "ok"
    assert child.trace_id == root.trace_id
    assert child.parent_span_id == root.span_id
    assert child.status == "ok"
    assert grandchild.parent_span_id == child.span_id
    assert grandchild.status == "error"
    assert grandchild.error_code == "operation_failed"
    assert "private provider body" not in str(grandchild.model_dump(mode="json"))


async def test_runtime_span_context_propagates_into_async_child_tasks():
    from app.observability.tracing import runtime_span

    buffer = RuntimeTraceBuffer()
    async with runtime_span("run.execute", run_id=RUN_ID, buffer=buffer):
        async def child():
            async with runtime_span("tool.execute", attributes={"tool_name": "read_file"}):
                await asyncio.sleep(0)

        await asyncio.create_task(child())

    spans = {span.operation: span for span in buffer.read_after(0, limit=10).items}
    root = spans["run.execute"]
    child_span = spans["tool.execute"]
    assert child_span.trace_id == root.trace_id
    assert child_span.parent_span_id == root.span_id


async def test_trace_emit_failure_does_not_replace_run_exception():
    from app.observability.tracing import runtime_span

    class BrokenBuffer:
        def append(self, **_fields):
            raise OSError("private disk detail")

    with pytest.raises(RuntimeError, match="run failure"):
        async with runtime_span("run.execute", run_id=RUN_ID, buffer=BrokenBuffer()):
            raise RuntimeError("run failure")


async def test_cancelled_span_uses_stable_category():
    from app.observability.tracing import runtime_span

    buffer = RuntimeTraceBuffer()
    with pytest.raises(asyncio.CancelledError):
        async with runtime_span("run.execute", run_id=RUN_ID, buffer=buffer):
            raise asyncio.CancelledError

    span = buffer.read_after(0, limit=1).items[0]
    assert span.status == "cancelled"
    assert span.error_code == "cancelled"
    assert span.error_category == "cancelled"


async def test_assistant_run_traces_model_call_without_capturing_answer(tmp_path):
    async with assistant_api(tmp_path, [decision("finish", {"result": {"answer": "private answer"}})]) as (
        app, client, session, _
    ):
        assert hasattr(app.state, "trace_buffer")
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()

        page = app.state.trace_buffer.read_after(0, limit=10)
        spans = {span.operation: span for span in page.items}
        root = spans["run.execute"]
        model = spans["model.complete"]

        assert str(root.run_id) == run_id
        assert model.trace_id == root.trace_id
        assert model.parent_span_id == root.span_id
        assert "private answer" not in str(page.model_dump(mode="json"))

        import httpx

        app.state.desktop_connection_installed = True
        app.state.desktop_clients = {"trace-test-client"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as trace_client:
            response = await trace_client.get(
                "/api/desktop/traces",
                headers={"X-Tomato-Client": "trace-test-client"},
                params={"after": 0},
            )
        assert response.status_code == 200
        assert {item["operation"] for item in response.json()["items"]} >= {
            "run.execute", "model.complete"
        }


async def test_assistant_memory_recall_is_traced_without_memory_text(tmp_path):
    async with assistant_api(
        tmp_path,
        [decision("finish", {"result": {"answer": "private answer"}})],
        memory_enabled=True,
    ) as (app, client, session, _):
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()

        page = app.state.trace_buffer.read_after(0, limit=20)
        memory = next(span for span in page.items if span.operation == "memory.recall")

        assert str(memory.run_id) == run_id
        assert "private answer" not in str(page.model_dump(mode="json"))


async def test_assistant_knowledge_tool_has_safe_trace_attributes(tmp_path):
    from test_assistant_api import decision

    responses = [
        decision("call_tool", {"name": "search_knowledge", "contract_version": "1",
                                "arguments": {"query": "private search phrase"}}),
        decision("finish", {"result": {"answer": "No local evidence."}}),
    ]
    async with assistant_api(tmp_path, responses) as (app, client, session, _):
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()

        page = app.state.trace_buffer.read_after(0, limit=20)
        knowledge = next(span for span in page.items if span.operation == "knowledge.search")

        assert str(knowledge.run_id) == run_id
        assert knowledge.attributes == {"tool_name": "search_knowledge"}
        assert "private search phrase" not in str(page.model_dump(mode="json"))


async def test_structured_tool_failure_is_not_recorded_as_success(tmp_path):
    from app.assistant.models import CapabilityError, CapabilityOutcome
    from app.tools.base import ToolResult
    from test_assistant_api import decision

    responses = [
        decision("call_tool", {"name": "search_knowledge", "contract_version": "1",
                                "arguments": {"query": "missing evidence"}}),
        decision("finish", {"result": {"answer": "Search failed."}}),
    ]
    async with assistant_api(tmp_path, responses) as (app, client, session, _):
        calls = []
        async def failed_search(_arguments, _context):
            calls.append(True)
            return ToolResult(success=False, data=CapabilityOutcome(
                kind="failed", error=CapabilityError(category="recoverable_error", code="search_failed")
            ).model_dump(mode="json"))

        app.state.task_service.assistant.registry.get("search_knowledge").execute = failed_search
        run_id = (await client.post(f"/api/sessions/{session}/tasks", json=request())).json()["run_id"]
        assert await app.state.execution_worker.run_once()
        assert len(calls) == 1

        page = app.state.trace_buffer.read_after(0, limit=20)
        span = next(item for item in page.items if item.operation == "knowledge.search")

        assert str(span.run_id) == run_id
        assert span.status == "error"
        assert span.error_code == "tool_failed"
        assert span.error_category == "knowledge"
