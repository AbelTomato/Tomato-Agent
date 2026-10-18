import json
from types import SimpleNamespace

import pytest

from app.observability.provider_attempt_recorder import ProviderAttemptRecorder


def test_recorder_persists_full_request_and_raw_response_without_headers(tmp_path):
    path = tmp_path / "attempts.jsonl"
    recorder = ProviderAttemptRecorder(path, 2, "run-123")
    payload = {
        "model": "model-x",
        "messages": [{"role": "user", "content": "private prompt"}],
        "tools": [{"type": "function", "function": {"name": "read_file"}}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "decision"}},
    }
    raw_body = b'{"id":"resp-1","choices":[{"message":{"content":"raw answer"}}]}'
    recorder.observe(payload, SimpleNamespace(status_code=200, content=raw_body))
    recorder.record["response"] = {"kind": "final", "content": "normalized answer"}
    recorder.write()

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["attempt"] == 2
    assert row["run_id"] == "run-123"
    assert row["run_association"] == "execution_context"
    assert row["request_payload"] == payload
    assert row["raw_response_body"] == raw_body.decode()
    assert row["http_status"] == 200
    assert row["response"] == {"kind": "final", "content": "normalized answer"}
    assert "headers" not in row
    assert "Authorization" not in json.dumps(row)
    assert "private prompt" in json.dumps(row)


def test_recorder_marks_missing_run_as_unknown_and_transport_failure(tmp_path):
    path = tmp_path / "attempts.jsonl"
    recorder = ProviderAttemptRecorder(path, 1, None)
    recorder.observe({"messages": []}, None)
    recorder.record["error_type"] = "ConnectError"
    recorder.write()

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["run_id"] is None
    assert row["run_association"] == "unknown"
    assert row["request_payload"] == {"messages": []}
    assert row["raw_response_body"] is None
    assert row["error_type"] == "ConnectError"


def test_recorder_persists_runtime_call_and_acceptance_associations(tmp_path):
    path = tmp_path / "attempts.jsonl"
    recorder = ProviderAttemptRecorder(
        path,
        3,
        "run-123",
        acceptance_id="acceptance-1",
        scenario_id="scenario-clarify",
        call_id="ledger-call-9",
        step_id="step-4",
        operation_id="operation-2",
    )
    recorder.observe({"model": "model-x"}, SimpleNamespace(status_code=200, content=b"{}"))
    recorder.record["response"] = {"kind": "final"}
    recorder.write()

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["acceptance_id"] == "acceptance-1"
    assert row["scenario_id"] == "scenario-clarify"
    assert row["run_id"] == "run-123"
    assert row["call_id"] == "ledger-call-9"
    assert row["step_id"] == "step-4"
    assert row["operation_id"] == "operation-2"
    assert row["correlation_missing"] is False


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [(503, "RuntimeError"), (200, "RuntimeError")],
    ids=["http-failure", "response-parse-failure"],
)
def test_recorder_preserves_call_association_for_http_and_parse_failures(
    tmp_path, status_code, error_type
):
    path = tmp_path / "attempts.jsonl"
    recorder = ProviderAttemptRecorder(path, 1, "run-1", call_id="ledger-call-1")
    recorder.observe({"model": "model-x"}, SimpleNamespace(status_code=status_code, content=b"not-json"))
    recorder.record["error_type"] = error_type
    recorder.write()

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["call_id"] == "ledger-call-1"
    assert row["correlation_missing"] is False
    assert row["http_status"] == status_code
    assert row["error_type"] == error_type


def test_recorder_marks_missing_call_identity_without_guessing(tmp_path):
    path = tmp_path / "attempts.jsonl"
    recorder = ProviderAttemptRecorder(path, 1, "run-1")
    recorder.observe({"model": "model-x"}, None)
    recorder.write()

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["call_id"] is None
    assert row["correlation_missing"] is True


def test_recorder_never_persists_credentials_or_authorization_headers(tmp_path):
    path = tmp_path / "attempts.jsonl"
    recorder = ProviderAttemptRecorder(path, 1, "run-1", call_id="ledger-call-1")
    recorder.observe(
        {"model": "model-x", "api_key": "secret-api-key", "Authorization": "Bearer secret-header"},
        SimpleNamespace(status_code=200, content=b'{"token":"secret-response-token"}'),
    )
    recorder.record["error"] = "Bearer secret-error-token"
    recorder.write()

    serialized = path.read_text(encoding="utf-8")
    for secret in ("secret-api-key", "secret-header", "secret-response-token", "secret-error-token"):
        assert secret not in serialized
    row = json.loads(serialized)
    assert row["request_payload"]["api_key"] == "[REDACTED]"
    assert row["request_payload"]["Authorization"] == "[REDACTED]"
    assert json.loads(row["raw_response_body"]) == {"token": "[REDACTED]"}


def test_recorder_preserves_authorization_policy_schema_fields(tmp_path):
    path = tmp_path / "attempts.jsonl"
    recorder = ProviderAttemptRecorder(path, 1, "run-1", call_id="ledger-call-1")
    payload = {
        "tools": [{"function": {"parameters": {
            "requires_execution_authorization": {"type": "boolean"},
            "authorization_header": {"type": "string"},
        }}}],
    }
    recorder.observe(payload, None)
    recorder.write()

    row = json.loads(path.read_text(encoding="utf-8"))
    parameters = row["request_payload"]["tools"][0]["function"]["parameters"]
    assert parameters["requires_execution_authorization"] == {"type": "boolean"}
    assert parameters["authorization_header"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_compatible_client_delivers_runtime_correlation_to_recorder(tmp_path):
    import httpx

    from app.agent.models import Message
    from app.llm.compatible_client import OpenAICompatibleClient, _REQUEST_CONTEXT, _REQUEST_CORRELATION

    path = tmp_path / "attempts.jsonl"
    recorder = ProviderAttemptRecorder(path, 1, "run-1", acceptance_id="acceptance-1", scenario_id="scenario-1")
    client = OpenAICompatibleClient(
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        ),
    )
    request_token = _REQUEST_CONTEXT.set(recorder.observe)
    correlation_token = _REQUEST_CORRELATION.set(
        {"run_id": "run-1", "call_id": "ledger-call-8", "step_id": "step-2", "operation_id": "op-3"}
    )
    try:
        await client.complete([Message(role="user", content="hello")], [])
    finally:
        _REQUEST_CORRELATION.reset(correlation_token)
        _REQUEST_CONTEXT.reset(request_token)
    recorder.write()

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["acceptance_id"] == "acceptance-1"
    assert row["scenario_id"] == "scenario-1"
    assert row["call_id"] == "ledger-call-8"
    assert row["step_id"] == "step-2"
    assert row["operation_id"] == "op-3"
    assert row["correlation_missing"] is False


@pytest.mark.parametrize("raw", [b"\xff", b""])
def test_recorder_preserves_decodeable_raw_body_even_when_not_json(tmp_path, raw):
    recorder = ProviderAttemptRecorder(tmp_path / "attempts.jsonl", 1, "run-1")
    recorder.observe({}, SimpleNamespace(status_code=502, content=raw))
    recorder.write()
    row = json.loads((tmp_path / "attempts.jsonl").read_text(encoding="utf-8"))
    assert row["raw_response_body"] == raw.decode("utf-8", errors="replace")
    assert row["raw_response_body_encoding"] == "utf-8-replace"
    assert row["http_status"] == 502