"""Create and score report-bound human reviews of generated memory summaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProjectionCaseReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    case_id: str = Field(min_length=1, max_length=120)
    preserved_must_preserve: list[str] = Field(default_factory=list, max_length=12)
    violated_must_not_infer: list[str] = Field(default_factory=list, max_length=12)
    preserved_temporal_constraints: list[str] = Field(default_factory=list, max_length=8)
    additional_unsupported_claims: list[str] = Field(default_factory=list, max_length=20)
    faithfulness_rating: int | None = Field(default=None, ge=1, le=5)


class ProjectionQualityReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: str
    report_sha256: str = Field(min_length=64, max_length=64)
    dataset_sha256: str = Field(min_length=64, max_length=64)
    reviewer_id: str = Field(min_length=1, max_length=120)
    reviewer_method: str = Field(min_length=1, max_length=300)
    cases: list[ProjectionCaseReview] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_review(self):
        if self.schema_version != "memory-projection-quality-review/v1":
            raise ValueError("unsupported projection quality review")
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate projection review case id")
        return self


def create_projection_review_template(report_path: Path, *, reviewer_id: str,
                                      reviewer_method: str) -> dict:
    report_path = report_path.expanduser().resolve(strict=True)
    if not report_path.is_file() or report_path.is_symlink():
        raise ValueError("projection report must be a regular non-symlink file")
    raw = report_path.read_bytes()
    report = json.loads(raw)
    if report.get("status") != "complete" or report.get("review_mode") != "human_review_required":
        raise ValueError("projection report is incomplete or not reviewable")
    if any(case.get("status") != "complete" for case in report.get("cases", [])):
        raise ValueError("projection report contains incomplete cases")
    return {
        "schema_version": "memory-projection-quality-review/v1",
        "report_sha256": hashlib.sha256(raw).hexdigest(),
        "dataset_sha256": report["dataset_sha256"],
        "reviewer_id": reviewer_id,
        "reviewer_method": reviewer_method,
        "cases": [{
            "case_id": case["case_id"],
            "preserved_must_preserve": [],
            "violated_must_not_infer": [],
            "preserved_temporal_constraints": [],
            "additional_unsupported_claims": [],
            "faithfulness_rating": None,
        } for case in report["cases"]],
    }


def render_projection_review_worksheet(report_path: Path) -> str:
    report_path = report_path.expanduser().resolve(strict=True)
    if not report_path.is_file() or report_path.is_symlink():
        raise ValueError("projection report must be a regular non-symlink file")
    raw = report_path.read_bytes()
    report = json.loads(raw)
    if report.get("status") != "complete" or report.get("review_mode") != "human_review_required":
        raise ValueError("projection report is incomplete or not reviewable")
    if any(case.get("status") != "complete" for case in report.get("cases", [])):
        raise ValueError("projection report contains incomplete cases")

    lines = [
        "# 独立人工摘要审阅工作表",
        "",
        f"报告 SHA256：`{hashlib.sha256(raw).hexdigest()}`",
        f"数据集 SHA256：`{report['dataset_sha256']}`",
        f"案例数：{len(report['cases'])}",
        "",
        "请逐例对照输入事实与生成摘要完成评分。`必须保留` 项若摘要表达了等价含义即勾选；",
        "`禁止推断` 项若摘要出现该推断才勾选；时间约束仅在含义和时序均保留时勾选。",
        "记录 rubric 未覆盖的无依据主张。忠实度评分：1=严重失真或危险推断，2=多项失真/遗漏，",
        "3=存在实质遗漏或歧义，4=基本忠实且仅有轻微问题，5=忠实、完整、无无依据主张。",
        "",
        "审阅者：",
        "审阅方法：独立人工逐例审阅；未使用模型辅助",
        "",
    ]
    for index, case in enumerate(report["cases"], start=1):
        criteria = case["review_criteria"]
        lines.extend([f"## {index}. {case['case_id']}", "", "输入事实："])
        lines.extend(f"- {fact['topic']}：{fact['fact']}" for fact in case["facts"])
        lines.extend(["", "生成摘要：", "", f"> {case['summary']}", "", "必须保留："])
        lines.extend(f"- [ ] {criterion}" for criterion in criteria["must_preserve"])
        lines.extend(["", "禁止推断（若发生请勾选）："])
        lines.extend(f"- [ ] {criterion}" for criterion in criteria["must_not_infer"])
        lines.extend(["", "时间约束（含义与时序均保留时勾选）："])
        lines.extend(f"- [ ] {criterion}" for criterion in criteria["temporal_constraints"])
        lines.extend([
            "", "其他无依据主张：", "- ", "", "忠实度评分（1-5）：", "", "说明：", "- ", "",
        ])
    return "\n".join(lines)


def score_projection_quality_review(report_path: Path, review_path: Path,
                                    *, output_path: Path | None = None) -> dict:
    report_path = report_path.expanduser().resolve(strict=True)
    review_path = review_path.expanduser().resolve(strict=True)
    report_raw = report_path.read_bytes()
    review_raw = review_path.read_bytes()
    report = json.loads(report_raw)
    review = ProjectionQualityReview.model_validate_json(review_raw)
    report_hash = hashlib.sha256(report_raw).hexdigest()
    if report_hash != review.report_sha256 or report.get("dataset_sha256") != review.dataset_sha256:
        raise ValueError("review does not match projection report")
    report_cases = {case["case_id"]: case for case in report.get("cases", [])}
    review_cases = {case.case_id: case for case in review.cases}
    if set(report_cases) != set(review_cases):
        raise ValueError("review cases do not match projection report")

    case_results = []
    must_preserve_total = must_preserve_met = 0
    forbidden_total = forbidden_violations = 0
    temporal_total = temporal_preserved = 0
    unsupported_claim_count = 0
    ratings = []
    for case_id, output_case in report_cases.items():
        annotations = review_cases[case_id]
        criteria = output_case["review_criteria"]
        groups = (
            (annotations.preserved_must_preserve, criteria["must_preserve"], "preserved_must_preserve"),
            (annotations.violated_must_not_infer, criteria["must_not_infer"], "violated_must_not_infer"),
            (annotations.preserved_temporal_constraints, criteria["temporal_constraints"],
             "preserved_temporal_constraints"),
        )
        for selected, available, label in groups:
            if len(selected) != len(set(selected)) or not set(selected) <= set(available):
                raise ValueError(f"invalid review annotations: {case_id}:{label}")

        required = len(criteria["must_preserve"])
        preserved = len(annotations.preserved_must_preserve)
        forbidden = len(criteria["must_not_infer"])
        violated = len(annotations.violated_must_not_infer)
        temporal = len(criteria["temporal_constraints"])
        temporal_kept = len(annotations.preserved_temporal_constraints)
        must_preserve_total += required
        must_preserve_met += preserved
        forbidden_total += forbidden
        forbidden_violations += violated
        temporal_total += temporal
        temporal_preserved += temporal_kept
        unsupported_claim_count += len(annotations.additional_unsupported_claims)
        if annotations.faithfulness_rating is not None:
            ratings.append(annotations.faithfulness_rating)
        case_results.append({
            "case_id": case_id,
            "must_preserve_count": required,
            "must_preserve_met": preserved,
            "must_preserve_missed": required - preserved,
            "must_not_infer_count": forbidden,
            "must_not_infer_violations": violated,
            "temporal_constraint_count": temporal,
            "temporal_constraints_preserved": temporal_kept,
            "additional_unsupported_claims": annotations.additional_unsupported_claims,
            "faithfulness_rating": annotations.faithfulness_rating,
        })

    scored = {
        "schema_version": "memory-projection-quality-score/v1",
        "status": "complete",
        "source_report_sha256": report_hash,
        "source_dataset_sha256": report["dataset_sha256"],
        "reviewer_id": review.reviewer_id,
        "reviewer_method": review.reviewer_method,
        "reviewer_independence": "self_reported_method_only",
        "summary": {
            "case_count": len(case_results),
            "must_preserve_total": must_preserve_total,
            "must_preserve_met": must_preserve_met,
            "must_preserve_missed": must_preserve_total - must_preserve_met,
            "must_preserve_coverage": round(must_preserve_met / must_preserve_total, 4)
            if must_preserve_total else None,
            "must_not_infer_total": forbidden_total,
            "must_not_infer_violations": forbidden_violations,
            "temporal_constraint_total": temporal_total,
            "temporal_constraints_preserved": temporal_preserved,
            "temporal_preservation_rate": round(temporal_preserved / temporal_total, 4)
            if temporal_total else None,
            "additional_unsupported_claim_count": unsupported_claim_count,
            "faithfulness_rating_mean": round(statistics.mean(ratings), 3) if ratings else None,
        },
        "cases": case_results,
    }
    if output_path is not None:
        output_path = output_path.expanduser().resolve()
        if output_path in {report_path, review_path}:
            raise ValueError("review score output must not overwrite an input")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(scored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return scored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.memory.projection_quality_review")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--template-output", type=Path)
    parser.add_argument("--worksheet-output", type=Path)
    parser.add_argument("--reviewer-id", default="reviewer")
    parser.add_argument("--reviewer-method", default="manual review against case criteria")
    parser.add_argument("--annotations", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    if args.template_output is not None:
        template = create_projection_review_template(
            args.report, reviewer_id=args.reviewer_id, reviewer_method=args.reviewer_method,
        )
        output = args.template_output.expanduser().resolve()
        if output == args.report.expanduser().resolve():
            parser.error("review template must not overwrite the source report")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"template": str(output), "case_count": len(template["cases"])}, ensure_ascii=False))
        return 0
    if args.worksheet_output is not None:
        worksheet = render_projection_review_worksheet(args.report)
        output = args.worksheet_output.expanduser().resolve()
        if output == args.report.expanduser().resolve():
            parser.error("review worksheet must not overwrite the source report")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(worksheet, encoding="utf-8")
        print(json.dumps({"worksheet": str(output),
                          "case_count": worksheet.count("## ")}, ensure_ascii=False))
        return 0
    if args.annotations is None or args.output is None:
        parser.error("--annotations and --output are required when scoring a review")
    result = score_projection_quality_review(args.report, args.annotations, output_path=args.output)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
