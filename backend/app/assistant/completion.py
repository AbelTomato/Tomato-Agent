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
    result = AssistantResult.model_validate(proposed_result)
    allowed = {item["citation_id"]: item for item in snapshot.evidence_refs}
    for citation in result.citations:
        if citation.model_dump(mode="json") != allowed.get(citation.citation_id):
            raise ValueError("invalid_citation")
    if result.validation_status != "not_required" or result.test_results or result.artifacts:
        raise ValueError("validation_not_available")
    return result