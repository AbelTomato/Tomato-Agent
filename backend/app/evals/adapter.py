"""Adapters from existing runtime models to the minimal result evaluator."""

from collections.abc import Iterable
from typing import Any

from app.agent.harness_models import ExecutionResult
from app.assistant.models import AssistantResult, RuntimeEvent

from .minimal_result import (
    EvidenceObservation,
    EvaluationInput,
    MinimalEvaluationReport,
    MinimalResultEvaluator,
    TaskEvaluationSpec,
    ToolCallObservation,
    UiObservation,
)


_TOOL_EVENT_TYPES = {"tool.started", "tool.succeeded", "tool.failed", "tool.unknown", "tool.waiting"}
_BLOCKING_SAFETY_CODES = {"permission_denied", "unknown_side_effect", "unauthorized_write"}


def _event_tool_name(event: RuntimeEvent) -> str:
    value = event.detail.data.get("tool_name") or event.detail.data.get("name")
    return str(value) if value else "unknown"


def _tool_observations(events: Iterable[RuntimeEvent]) -> tuple[ToolCallObservation, ...]:
    observations: list[ToolCallObservation] = []
    for event in events:
        if event.event_type not in _TOOL_EVENT_TYPES or event.event_type == "tool.started":
            continue
        observations.append(
            ToolCallObservation(
                name=_event_tool_name(event),
                protocol_valid=event.event_type != "tool.unknown",
                completed=event.event_type in {"tool.succeeded", "tool.failed"},
            )
        )
    return tuple(observations)


def _evidence_observations(result: AssistantResult | None) -> tuple[EvidenceObservation, ...]:
    if result is None:
        return ()
    evidence: list[EvidenceObservation] = []
    for citation in result.citations:
        citation_id = getattr(citation, "citation_id", None) or getattr(citation, "source_id", None)
        if citation_id:
            evidence.append(EvidenceObservation(ref=f"citation:{citation_id}", kind="citation"))
    for artifact in result.artifacts:
        evidence.append(EvidenceObservation(ref=f"artifact:{artifact.artifact_id}", kind="artifact"))
    for index, test_result in enumerate(result.test_results):
        passed = test_result.get("passed") is True
        evidence.append(
            EvidenceObservation(ref=f"test-result:{index}", kind="test_result", valid=passed)
        )
    return tuple(evidence)


def build_evaluation_input(
    execution_result: ExecutionResult,
    *,
    assistant_result: AssistantResult | None = None,
    runtime_events: Iterable[RuntimeEvent] = (),
    ui_observation: UiObservation | None = None,
) -> EvaluationInput:
    """Convert existing execution facts without inventing missing observations."""

    events = tuple(runtime_events)
    safety_violations: list[str] = []
    blocking_safety_violations: list[str] = []
    for event in events:
        code = event.detail.data.get("code")
        if event.event_type in {"tool.unknown", "tool.failed"} and code:
            code = str(code)
            safety_violations.append(code)
            if code in _BLOCKING_SAFETY_CODES:
                blocking_safety_violations.append(code)

    output: Any = assistant_result.model_dump(mode="json") if assistant_result else execution_result.output
    return EvaluationInput(
        execution_status=execution_result.status,
        output=output,
        tool_calls=_tool_observations(events),
        evidence=_evidence_observations(assistant_result),
        safety_violations=tuple(safety_violations),
        blocking_safety_violations=tuple(blocking_safety_violations),
        ui=ui_observation or UiObservation(),
        total_tokens=execution_result.usage.total_tokens,
        duration_ms=execution_result.duration_ms,
    )


def evaluate_execution(
    spec: TaskEvaluationSpec,
    execution_result: ExecutionResult,
    *,
    assistant_result: AssistantResult | None = None,
    runtime_events: Iterable[RuntimeEvent] = (),
    ui_observation: UiObservation | None = None,
    evaluator: MinimalResultEvaluator | None = None,
) -> MinimalEvaluationReport:
    """Evaluate a completed runtime result as an explicit, side-channel operation."""

    observation = build_evaluation_input(
        execution_result,
        assistant_result=assistant_result,
        runtime_events=runtime_events,
        ui_observation=ui_observation,
    )
    return (evaluator or MinimalResultEvaluator()).evaluate(spec, observation)