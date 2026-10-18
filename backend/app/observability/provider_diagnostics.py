"""Local, opt-in provider evidence. Categories describe observations, not blame."""

import json
import logging
from collections import Counter
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)
RESPONSE_HEADERS = {"content-type", "x-request-id", "request-id", "x-trace-id", "traceparent", "retry-after", "server", "cf-ray"}


def error_group(category: str) -> str:
    if category in {"http_401", "http_403"}:
        return "authentication_permission"
    if category == "http_429":
        return "rate_limit"
    if category == "http_408":
        return "http_timeout"
    if category.startswith("http_5"):
        return "http_server_error"
    if category.startswith("http_4"):
        return "http_request_error"
    if category.startswith("transport_"):
        return "transport"
    return category


def classify(status: int | None, error: BaseException | None, phase: str) -> str | None:
    if status is not None and status >= 400:
        return f"http_{status}"
    if error is None:
        return None
    if isinstance(error, httpx.TimeoutException):
        return f"transport_{type(error).__name__}"
    if isinstance(error, httpx.TransportError):
        return f"transport_{type(error).__name__}"
    if isinstance(error, httpx.InvalidURL):
        return "request_configuration"
    if type(error).__name__ == "CancelledError":
        return "local_cancelled"
    return "response_protocol" if phase in {"parse", "stream"} else "local_client_error"


def request_shape(payload: dict, request_bytes: int | None) -> dict:
    messages = payload.get("messages", [])
    pending: set[str] = set()
    issues = []
    for index, message in enumerate(messages):
        if message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                issues.append({"message_index": index, "issue": "unmatched_tool_result"})
            pending.discard(call_id)
        else:
            if pending:
                issues.append({"message_index": index, "issue": "missing_tool_result"})
                pending.clear()
            pending.update(call.get("id") for call in message.get("tool_calls", []) if call.get("id"))
    if pending:
        issues.append({"message_index": len(messages), "issue": "missing_tool_result"})
    return {"message_count": len(messages), "roles": [m.get("role") for m in messages],
            "tool_count": len(payload.get("tools", [])), "request_bytes": request_bytes,
            "tool_pairing_issues": issues}


def write_error_report(attempts_path: Path) -> None:
    """Regenerate derived indexes from the authoritative attempts file."""
    rows = [json.loads(line) for line in attempts_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    failures = []
    previous = {}
    for line_number, row in enumerate(rows, 1):
        diagnostic = row.get("diagnostic", {})
        key = (row.get("run_id"), diagnostic.get("endpoint"), diagnostic.get("model"))
        category = diagnostic.get("error_category")
        if category:
            prior = previous.get(key)
            shape = diagnostic.get("request_shape", {})
            prior_shape = prior.get("diagnostic", {}).get("request_shape", {}) if prior else {}
            old_payload = prior.get("request_payload", {}) if prior else {}
            payload = row.get("request_payload", {})
            failures.append({"attempt_line": line_number, "run_id": row.get("run_id"), "call_id": row.get("call_id"),
                             "http_status": row.get("http_status"), "category": category,
                             "group": error_group(category),
                             "root_cause": "undetermined", "previous_success_call_id": prior.get("call_id") if prior else None,
                             "changed_payload_fields": sorted(k for k in payload.keys() | old_payload.keys() if payload.get(k) != old_payload.get(k)) if prior else None,
                             "message_count_delta": shape.get("message_count", 0) - prior_shape.get("message_count", 0) if prior else None,
                             "diagnostic": diagnostic})
        elif "diagnostic" in row:
            previous[key] = row
    root = attempts_path.parent
    (root / "provider-errors.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in failures), encoding="utf-8")
    lines = ["# Provider 调用诊断报告", "", f"实际请求记录：{len(rows)}；失败：{len(failures)}。",
             "", "分类表示观察事实；根因未确定。完整脱敏请求/响应见 provider-attempts.jsonl 对应行。",
             "缺失的阶段耗时不估算；无自动重试。", "", "| 类别 | 次数 |", "| --- | --- |"]
    lines.extend(f"| {category} | {count} |" for category, count in Counter(f["category"] for f in failures).items())
    for failure in failures:
        lines.extend(["", f"## 请求记录第 {failure['attempt_line']} 行", "", "```json",
                      json.dumps(failure, ensure_ascii=False, indent=2), "```"])
    temporary = root / "provider-error-report.md.pending"
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(root / "provider-error-report.md")


def diagnostic_warning() -> None:
    logger.warning("provider_diagnostics_write_failed")