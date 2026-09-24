"""Paired comparison of persisted retrieval benchmark reports."""

from __future__ import annotations

import json
import math
from pathlib import Path

from app.knowledge.benchmark_models import (
    ComparisonReport,
    MetricDelta,
    QuestionDelta,
    RunReport,
)


_METRIC_DIRECTIONS = {
    "recall_at_1": 1,
    "recall_at_3": 1,
    "recall_at_5": 1,
    "mrr_at_5": 1,
    "hit_at_5": 1,
    "unanswerable_empty_result_rate": 1,
    "answerable_empty_result_rate": -1,
    "unanswerable_nonempty_rate": -1,
    "execution_error_rate": -1,
}


def _metric_value(report: RunReport, name: str) -> float | None:
    value = report.summary.get(name)
    if isinstance(value, dict):
        value = value.get("value")
    if value is None:
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"metric {name} must be finite")
    return numeric


def _quality(item) -> dict[str, float | bool | None]:
    return {
        "recall_at_1": item.recall_at_1,
        "recall_at_3": item.recall_at_3,
        "recall_at_5": item.recall_at_5,
        "mrr_at_5": item.reciprocal_rank,
        "hit_at_5": item.hit,
        "unanswerable_empty_result_rate": (
            1.0 if not item.answerable and not item.retrieved_chunk_ids else
            0.0 if not item.answerable else None
        ),
        "answerable_empty_result_rate": (
            1.0 if item.answerable and not item.retrieved_chunk_ids else
            0.0 if item.answerable else None
        ),
        "unanswerable_nonempty_rate": (
            1.0 if not item.answerable and item.retrieved_chunk_ids else
            0.0 if not item.answerable else None
        ),
    }


def compare_reports(baseline: RunReport, candidate: RunReport) -> ComparisonReport:
    if baseline.status != "complete" or candidate.status != "complete":
        return ComparisonReport(
            schema_version="benchmark-comparison/v1",
            baseline_run_id=baseline.run_id,
            candidate_run_id=candidate.run_id,
            status="incomplete",
            reason="one or both runs are incomplete or failed",
            metric_deltas={},
            question_deltas=(),
        )

    incompatibilities: list[str] = []
    if baseline.metric_version != candidate.metric_version:
        incompatibilities.append("metric versions differ")
    if baseline.dataset_sha256 != candidate.dataset_sha256:
        incompatibilities.append("dataset fingerprints differ")
    if baseline.corpus_sha256 != candidate.corpus_sha256:
        incompatibilities.append("corpus fingerprints differ")
    if baseline.manifest.split != candidate.manifest.split:
        incompatibilities.append("splits differ")
    index_changed = baseline.index_sha256 != candidate.index_sha256
    scopes_match = baseline.manifest.comparison_scope == candidate.manifest.comparison_scope
    if not scopes_match:
        incompatibilities.append("comparison scopes differ")
    if baseline.manifest.comparison_scope == "fixed_index" and index_changed:
        incompatibilities.append("index fingerprints differ under fixed_index scope")
    if baseline.manifest.comparison_scope == "index_strategy" and index_changed:
        if "index" not in baseline.manifest.allowed_changes or "index" not in candidate.manifest.allowed_changes:
            incompatibilities.append("index change was not declared by both manifests")
    if incompatibilities:
        return ComparisonReport(
            schema_version="benchmark-comparison/v1",
            baseline_run_id=baseline.run_id,
            candidate_run_id=candidate.run_id,
            status="incomparable",
            reason="; ".join(incompatibilities),
            metric_deltas={},
            question_deltas=(),
        )

    baseline_questions = {item.question_id: item for item in baseline.questions}
    candidate_questions = {item.question_id: item for item in candidate.questions}
    if set(baseline_questions) != set(candidate_questions):
        raise ValueError("run question IDs must match for paired comparison")

    deltas: dict[str, MetricDelta] = {}
    improved = regressed = False
    for name, direction in _METRIC_DIRECTIONS.items():
        left, right = _metric_value(baseline, name), _metric_value(candidate, name)
        absolute = right - left if left is not None and right is not None else None
        relative = (
            absolute / abs(left)
            if absolute is not None and left not in (None, 0.0)
            else None
        )
        deltas[name] = MetricDelta(
            baseline=left,
            candidate=right,
            absolute=absolute,
            relative=relative,
        )
        if absolute is not None:
            improved |= direction * absolute > 1e-12
            regressed |= direction * absolute < -1e-12

    question_deltas: list[QuestionDelta] = []
    for question_id in sorted(baseline_questions):
        left, right = baseline_questions[question_id], candidate_questions[question_id]
        if left.status != "complete" or right.status != "complete":
            classification = "error"
        else:
            left_recall = left.recall if left.recall is not None else 0.0
            right_recall = right.recall if right.recall is not None else 0.0
            if left.answerable != right.answerable:
                raise ValueError(f"question answerability differs for {question_id}")
            if left.answerable and abs(right_recall - left_recall) > 1e-12:
                classification = "improved" if right_recall > left_recall else "regressed"
            elif not left.answerable and bool(left.retrieved_chunk_ids) != bool(right.retrieved_chunk_ids):
                classification = "improved" if not right.retrieved_chunk_ids else "regressed"
            else:
                classification = "unchanged"
        question_deltas.append(
            QuestionDelta(
                question_id=question_id,
                baseline_status=left.status,
                candidate_status=right.status,
                classification=classification,
            )
        )

    status = "mixed" if improved and regressed else "improved" if improved else "regressed" if regressed else "unchanged"
    return ComparisonReport(
        schema_version="benchmark-comparison/v1",
        baseline_run_id=baseline.run_id,
        candidate_run_id=candidate.run_id,
        status=status,
        reason="descriptive metric comparison; not a statistical significance or release decision",
        metric_deltas=deltas,
        question_deltas=tuple(question_deltas),
    )


def write_comparison(report: ComparisonReport, output_dir: Path) -> tuple[Path, Path]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    json_path = output_dir / "comparison.json"
    markdown_path = output_dir / "comparison.md"
    payload = report.model_dump(mode="json")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Benchmark 比较报告",
        "",
        f"- 状态：`{report.status}`",
        f"- Baseline：`{report.baseline_run_id}`",
        f"- Candidate：`{report.candidate_run_id}`",
        f"- 说明：{report.reason}",
        "",
        "| 指标 | Baseline | Candidate | 绝对变化 | 相对变化 |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, delta in report.metric_deltas.items():
        values = (delta.baseline, delta.candidate, delta.absolute, delta.relative)
        rendered = ["null" if value is None else f"{value:.12g}" for value in values]
        lines.append(f"| {name} | " + " | ".join(rendered) + " |")
    lines.extend(["", "## 逐题变化", "", "| 题目 | 状态 |", "|---|---|"])
    lines.extend(f"| {item.question_id} | {item.classification} |" for item in report.question_deltas)
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path