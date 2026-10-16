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


@pytest.mark.parametrize("raw", [b"\xff", b""])
def test_recorder_preserves_decodeable_raw_body_even_when_not_json(tmp_path, raw):
    recorder = ProviderAttemptRecorder(tmp_path / "attempts.jsonl", 1, "run-1")
    recorder.observe({}, SimpleNamespace(status_code=502, content=raw))
    recorder.write()
    row = json.loads((tmp_path / "attempts.jsonl").read_text(encoding="utf-8"))
    assert row["raw_response_body"] == raw.decode("utf-8", errors="replace")
    assert row["raw_response_body_encoding"] == "utf-8-replace"
    assert row["http_status"] == 502