"""Minimal, result-oriented evaluation for agent executions.

The evaluator intentionally does not compare a trace with one canonical path.
It checks task invariants, protocol constraints, evidence, safety and budgets;
any tool sequence satisfying those constraints is accepted.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Status = Literal["pass", "partial", "fail", "not_applicable"]


class _EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TaskEvaluationSpec(_EvaluationModel):
    """Result invariants and constraints for one task."""

    required_output_fields: frozenset[str] = frozenset()
    required_evidence_refs: frozenset[str] = frozenset()
    allowed_tools: frozenset[str] = frozenset()
    ui_required: bool = False
    max_tool_calls: int | None = Field(default=None, gt=0)
    max_duration_ms: float | None = Field(default=None, gt=0)
    max_total_tokens: int | None = Field(default=None, gt=0)


class ToolCallObservation(_EvaluationModel):
    name: str = Field(min_length=1)
    protocol_valid: bool = True
    completed: bool = True


class EvidenceObservation(_EvaluationModel):
    ref: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    valid: bool = True
    covers: frozenset[str] = frozenset()


class UiObservation(_EvaluationModel):
    applicable: bool = False
    completed: bool = False
    issues: tuple[str, ...] = ()


class EvaluationInput(_EvaluationModel):
    execution_status: Literal["completed", "stopped", "failed", "waiting"]
    output: Any | None = None
    tool_calls: tuple[ToolCallObservation, ...] = ()
    evidence: tuple[EvidenceObservation, ...] = ()
    safety_violations: tuple[str, ...] = ()
    blocking_safety_violations: tuple[str, ...] = ()
    ui: UiObservation = UiObservation()
    total_tokens: int = Field(default=0, ge=0)
    duration_ms: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_safety_violations(self) -> "EvaluationInput":
        if not set(self.blocking_safety_violations).issubset(self.safety_violations):
            raise ValueError("blocking safety violations must be reported as violations")
        return self


class DimensionResult(_EvaluationModel):
    status: Status
    score: float = Field(ge=0, le=1)
    reasons: tuple[str, ...] = ()
    blocking: bool = False


class MinimalEvaluationReport(_EvaluationModel):
    task: DimensionResult
    protocol: DimensionResult
    evidence: DimensionResult
    safety: DimensionResult
    budget: DimensionResult
    ui: DimensionResult
    overall: DimensionResult


class MinimalResultEvaluator:
    """Evaluate outcomes by invariants, accepting different legal tool paths."""

    DEFAULT_WEIGHTS = {
        "task": 0.35,
        "protocol": 0.15,
        "evidence": 0.15,
        "safety": 0.20,
        "budget": 0.10,
        "ui": 0.05,
    }

    def __init__(self, weights: dict[str, float] | None = None) -> None:
        self.weights = dict(weights or self.DEFAULT_WEIGHTS)
        unknown = set(self.weights) - set(self.DEFAULT_WEIGHTS)
        if unknown or any(value < 0 for value in self.weights.values()):
            raise ValueError("weights must be non-negative known dimensions")
        if not any(self.weights.values()):
            raise ValueError("at least one weight must be positive")

    def evaluate(
        self,
        spec: TaskEvaluationSpec,
        observation: EvaluationInput,
    ) -> MinimalEvaluationReport:
        dimensions = {
            "task": self._task(spec, observation),
            "protocol": self._protocol(spec, observation),
            "evidence": self._evidence(spec, observation),
            "safety": self._safety(observation),
            "budget": self._budget(spec, observation),
            "ui": self._ui(spec, observation),
        }
        overall = self._overall(dimensions)
        return MinimalEvaluationReport(**dimensions, overall=overall)

    @staticmethod
    def _task(spec: TaskEvaluationSpec, observation: EvaluationInput) -> DimensionResult:
        reasons: list[str] = []
        if observation.execution_status != "completed":
            reasons.append(f"execution status is {observation.execution_status}")
        if spec.required_output_fields:
            if not isinstance(observation.output, dict):
                reasons.append("output is not an object with required fields")
            else:
                missing = spec.required_output_fields - set(observation.output)
                if missing:
                    reasons.append(f"missing output fields: {', '.join(sorted(missing))}")
        status: Status = "fail" if reasons else "pass"
        return DimensionResult(status=status, score=0 if reasons else 1, reasons=tuple(reasons), blocking=status == "fail")

    @staticmethod
    def _protocol(spec: TaskEvaluationSpec, observation: EvaluationInput) -> DimensionResult:
        reasons: list[str] = []
        invalid = [call.name for call in observation.tool_calls if not call.protocol_valid]
        if invalid:
            reasons.append(f"invalid tool calls: {', '.join(invalid)}")
        if spec.allowed_tools:
            disallowed = sorted({call.name for call in observation.tool_calls} - spec.allowed_tools)
            if disallowed:
                reasons.append(f"disallowed tools: {', '.join(disallowed)}")
        status: Status = "fail" if reasons else "pass"
        return DimensionResult(status=status, score=0 if reasons else 1, reasons=tuple(reasons), blocking=status == "fail")

    @staticmethod
    def _evidence(spec: TaskEvaluationSpec, observation: EvaluationInput) -> DimensionResult:
        valid_refs = {item.ref for item in observation.evidence if item.valid}
        missing = spec.required_evidence_refs - valid_refs
        reasons = (f"missing evidence refs: {', '.join(sorted(missing))}",) if missing else ()
        status: Status = "fail" if missing else "pass"
        return DimensionResult(status=status, score=0 if missing else 1, reasons=reasons, blocking=False)

    @staticmethod
    def _safety(observation: EvaluationInput) -> DimensionResult:
        reasons = tuple(observation.safety_violations)
        blocking = bool(observation.blocking_safety_violations)
        status: Status = "fail" if blocking else ("partial" if reasons else "pass")
        return DimensionResult(status=status, score=0 if blocking else (0.5 if reasons else 1), reasons=reasons, blocking=blocking)

    @staticmethod
    def _budget(spec: TaskEvaluationSpec, observation: EvaluationInput) -> DimensionResult:
        reasons: list[str] = []
        if spec.max_tool_calls is not None and len(observation.tool_calls) > spec.max_tool_calls:
            reasons.append("tool call budget exceeded")
        if spec.max_duration_ms is not None and observation.duration_ms > spec.max_duration_ms:
            reasons.append("duration budget exceeded")
        if spec.max_total_tokens is not None and observation.total_tokens > spec.max_total_tokens:
            reasons.append("token budget exceeded")
        status: Status = "fail" if reasons else "pass"
        return DimensionResult(status=status, score=0 if reasons else 1, reasons=tuple(reasons))

    @staticmethod
    def _ui(spec: TaskEvaluationSpec, observation: EvaluationInput) -> DimensionResult:
        if not spec.ui_required and not observation.ui.applicable:
            return DimensionResult(status="not_applicable", score=1)
        if not observation.ui.completed:
            return DimensionResult(status="fail", score=0, reasons=("UI task was not completed",))
        if observation.ui.issues:
            return DimensionResult(status="partial", score=0.5, reasons=observation.ui.issues)
        return DimensionResult(status="pass", score=1)

    def _overall(self, dimensions: dict[str, DimensionResult]) -> DimensionResult:
        blocking = [name for name, result in dimensions.items() if result.blocking]
        failures = [name for name, result in dimensions.items() if result.status == "fail"]
        applicable = [name for name, result in dimensions.items() if result.status != "not_applicable"]
        weight_total = sum(self.weights.get(name, 0) for name in applicable)
        score = sum(self.weights.get(name, 0) * dimensions[name].score for name in applicable) / weight_total
        reasons = tuple(f"{name} failed" for name in failures)
        if blocking or dimensions["task"].status == "fail":
            status: Status = "fail"
        elif failures or any(dimensions[name].status == "partial" for name in applicable):
            status = "partial"
        else:
            status = "pass"
        return DimensionResult(status=status, score=score, reasons=reasons, blocking=bool(blocking))