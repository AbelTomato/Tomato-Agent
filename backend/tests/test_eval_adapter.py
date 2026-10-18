from datetime import datetime, timezone
from uuid import uuid4

from app.agent.harness_models import ExecutionResult, UsageSnapshot
from app.assistant.models import AssistantResult, RuntimeEvent, RuntimeEventDetail
from app.evals import (
    TaskEvaluationSpec,
    build_evaluation_input,
    evaluate_execution,
)


def runtime_event(event_type: str, **data) -> RuntimeEvent:
    return RuntimeEvent(
        event_id=uuid4(),
        run_id=uuid4(),
        event_type=event_type,
        occurred_at=datetime.now(timezone.utc),
        summary=event_type,
        detail=RuntimeEventDetail(data=data),
    )


def execution_result(**changes) -> ExecutionResult:
    values = {
        "status": "completed",
        "output": {"answer": "ok"},
        "usage": UsageSnapshot(total_tokens=12),
        "tool_calls": 1,
        "duration_ms": 25,
    }
    values.update(changes)
    return ExecutionResult(
        **values,
    )


def test_adapter_maps_tool_events_and_assistant_evidence():
    observation = build_evaluation_input(
        execution_result(),
        assistant_result=AssistantResult(test_results=[{"passed": True}]),
        runtime_events=(
            runtime_event("tool.started", tool_name="read_file"),
            runtime_event("tool.succeeded", tool_name="read_file"),
        ),
    )

    assert [call.name for call in observation.tool_calls] == ["read_file"]
    assert observation.tool_calls[0].protocol_valid is True
    assert observation.evidence[0].ref == "test-result:0"
    assert observation.total_tokens == 12


def test_adapter_marks_unknown_side_effect_as_blocking_safety_violation():
    observation = build_evaluation_input(
        execution_result(status="waiting"),
        runtime_events=(
            runtime_event("tool.unknown", tool_name="publish", code="unknown_side_effect"),
        ),
    )

    assert observation.safety_violations == ("unknown_side_effect",)
    assert observation.blocking_safety_violations == ("unknown_side_effect",)


def test_evaluate_execution_is_explicit_and_does_not_change_execution_status():
    report = evaluate_execution(
        TaskEvaluationSpec(required_output_fields=frozenset({"answer"})),
        execution_result(),
    )

    assert report.overall.status == "pass"
    assert report.task.status == "pass"