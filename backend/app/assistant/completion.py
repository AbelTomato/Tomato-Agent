"""Read-only completion and evidence membership gate (write gates follow later)."""

import json

from .models import AssistantResult


def validate_completion(snapshot, proposed_result):
    if isinstance(proposed_result, str):
        try:
            payload = json.loads(proposed_result)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            allowed = {item["citation_id"]: item for item in snapshot.evidence_refs}
            ids = payload.get("citation_ids", [])
            if not isinstance(ids, list) or any(not isinstance(item, str) or item not in allowed for item in ids):
                raise ValueError("invalid_citation")
            if len(ids) != len(set(ids)):
                raise ValueError("invalid_citation")
            payload = {**payload, "citations": [allowed[item] for item in ids]}
            payload.pop("citation_ids", None)
            proposed_result = AssistantResult.model_validate(payload)
        else:
            proposed_result = AssistantResult(answer=proposed_result)
    result = (proposed_result if isinstance(proposed_result, AssistantResult)
        else AssistantResult.model_validate_json(json.dumps(proposed_result)))
    allowed = {item["citation_id"]: item for item in snapshot.evidence_refs}
    for citation in result.citations:
        if citation.model_dump(mode="json") != allowed.get(citation.citation_id):
            raise ValueError("invalid_citation")
    if any(report not in snapshot.test_results for report in result.test_results):
        raise ValueError("invalid_test_result")
    observed_artifacts = [report.get("artifact") for report in snapshot.test_results if report.get("artifact")]
    if any(artifact.model_dump(mode="json") not in observed_artifacts for artifact in result.artifacts):
        raise ValueError("validation_not_available")
    if result.validation_status in {"passed", "failed"}:
        reports = result.test_results
        if not reports or any(type(report.get("passed")) is not bool for report in reports):
            raise ValueError("validation_not_available")
        expected = "passed" if all(report["passed"] for report in reports) else "failed"
        if result.validation_status != expected:
            raise ValueError("invalid_validation_status")
    elif result.validation_status == "unavailable":
        raise ValueError("validation_not_available")
    return result