import json
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, ValidationError

from .models import ExecutionModel, ExecutionSnapshot, OperationStatus
from .ports import canonical_digest


class RecoveryConflict(RuntimeError):
    """The observed execution state no longer permits recovery."""


class RecoveryOperation(ExecutionModel):
    id: UUID
    logical_index: int = Field(ge=0)
    kind: str
    tool_name: str | None = None
    recovery_class: Literal["model", "read_only", "file_write", "artifact", "sandbox"]
    status: OperationStatus
    input_digest: str
    input_payload: dict[str, Any]
    before_digests: dict[str, str] = Field(default_factory=dict)
    after_digests: dict[str, str] = Field(default_factory=dict)
    result_payload: dict[str, Any] | None = None
    output_digest: str | None = None


class RecoveryObservation(ExecutionModel):
    policy_digest: str
    executor_exited: bool = False
    file_digests: dict[str, str] = Field(default_factory=dict)
    artifact_refs: list[dict[str, str]] = Field(default_factory=list)
    sandbox_reconciled: bool = False
    sandbox_retry_approved: bool = False


class RecoveryDecision(ExecutionModel):
    action: Literal["reuse", "retry", "reconcile", "manual_review", "reject"]
    reason_code: str
    observation: RecoveryObservation
    result_payload: dict[str, Any] | None = None


class RecoveryCandidate(ExecutionModel):
    run_id: UUID
    attempt_id: UUID
    run_version: int
    fencing_token: int
    checkpoint_sequence: int
    snapshot: dict[str, Any] | None
    operation: RecoveryOperation | None
    state_digest: str


def classify_recovery(snapshot, operation, observation: RecoveryObservation) -> RecoveryDecision:
    def decision(action, reason, result=None):
        return RecoveryDecision(action=action, reason_code=reason,
                                observation=observation, result_payload=result)

    if snapshot is None:
        return decision("reject", "legacy_snapshot")
    raw = snapshot.model_dump(mode="json") if isinstance(snapshot, ExecutionSnapshot) else snapshot
    if raw.get("protocol_version") != 1:
        return decision("reject", "unsupported_protocol")
    if raw.get("schema_version") != 1:
        return decision("reject", "unsupported_snapshot")
    try:
        snap = ExecutionSnapshot.model_validate_json(json.dumps(raw))
    except (ValidationError, TypeError, ValueError):
        return decision("reject", "invalid_snapshot")
    if snap.policy_digest != observation.policy_digest:
        return decision("reject", "policy_mismatch")
    if operation is None:
        if snap.pending_operation_id is not None:
            return decision("reject", "operation_not_found")
        if snap.phase == "pending_tool" and (
                snap.pending_response is None or snap.pending_response.kind != "tool_call"
                or snap.pending_response.tool_call is None):
            return decision("reject", "invalid_snapshot")
        return decision("reuse" if snap.phase in {"pending_tool", "ready_finish"} else "retry",
                        "snapshot_resumable")
    if canonical_digest(operation.input_payload) != operation.input_digest:
        return decision("reject", "operation_input_conflict")
    if operation.status == "succeeded":
        return decision("reuse", "persisted_result", operation.result_payload)
    if operation.status == "failed":
        return decision("reject", "operation_failed")
    if operation.recovery_class in {"model", "read_only"}:
        return decision("retry", "safe_retry")
    if operation.recovery_class == "file_write" and observation.executor_exited:
        before, after = operation.before_digests, operation.after_digests
        if before and before.keys() == after.keys():
            actual = {key: observation.file_digests.get(key) for key in before}
            if actual == after:
                return decision("reconcile", "file_postimage_confirmed", operation.result_payload)
            if actual == before:
                return decision("retry", "file_preimage_confirmed")
    if operation.recovery_class == "artifact" and operation.output_digest:
        matches = [ref for ref in observation.artifact_refs
                   if ref.get("operation_id") == str(operation.id)
                   and ref.get("content_digest") == operation.output_digest]
        if len(matches) == 1:
            return decision("reconcile", "unique_artifact_confirmed", matches[0])
    if (operation.recovery_class == "sandbox" and observation.executor_exited
            and observation.sandbox_reconciled and observation.sandbox_retry_approved):
        return decision("retry", "sandbox_retry_confirmed")
    return decision("manual_review", "unknown_side_effect")