"""Evaluation helpers for agent execution results."""

from .minimal_result import (
    DimensionResult,
    EvidenceObservation,
    EvaluationInput,
    MinimalEvaluationReport,
    MinimalResultEvaluator,
    TaskEvaluationSpec,
    ToolCallObservation,
    UiObservation,
)
from .adapter import build_evaluation_input, evaluate_execution

__all__ = [
    "DimensionResult",
    "EvidenceObservation",
    "EvaluationInput",
    "MinimalEvaluationReport",
    "MinimalResultEvaluator",
    "TaskEvaluationSpec",
    "ToolCallObservation",
    "UiObservation",
    "build_evaluation_input",
    "evaluate_execution",
]