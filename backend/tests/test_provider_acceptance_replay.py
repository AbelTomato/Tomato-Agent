import json
import subprocess
import sys

import httpx
import pytest

from app.agent.models import LLMResponse, Message, ToolCall, ToolDefinition
from tests.fixtures.provider_acceptance_runtime import CappedProvider
from fixtures.provider_acceptance_replay import ProviderReplayClient, ReplayError, write_replay_summary
from fixtures.provider_acceptance_replay import _message_payload, _tool_payload


@pytest.mark.asyncio
async def test_capped_provider_preserves_structured_response_format(monkeypatch, tmp_path):
    response_format = {"type": "json_schema", "json_schema": {"name": "decision"}}
    captured = {}

    class Client:
        response_format_identity = response_format
        calls = 0

        async def complete(self, messages, tools, *, response_format=None):
            captured["response_format"] = response_format
            self.calls += 1
            return LLMResponse(kind="final", content="ok")

    monkeypatch.setenv("RUNTIME_ACCEPTANCE_MAX_CALLS", "2")
    monkeypatch.setenv("RUNTIME_ACCEPTANCE_MODE", "replay")
    monkeypatch.setenv("RUNTIME_ACCEPTANCE_ROOT", str(tmp_path))
    provider = CappedProvider(Client())
    assert provider.response_format_identity == response_format
    result = await provider.complete([], [], response_format=response_format)
    assert result.content == "ok"
    assert captured["response_format"] == response_format


def _payload(messages, tools, response_format=None):
    payload = {
        "model": "recorded-model",
        "messages": [_message_payload(item) for item in messages],
    }
    if tools:
        payload["tools"] = [_tool_payload(item) for item in tools]
        payload["tool_choice"] = "auto"
        payload["parallel_tool_calls"] = False
    if response_format is not None:
        payload["response_format"] = response_format
    return payload


def _record(call_id, messages, response=None, *, tools=(), status=200, error_type=None):
    return {
        "call_id": call_id,
        "request_payload": _payload(messages, list(tools)),
        "http_status": status,
        "response": response,
        "error_type": error_type,
    }


def _write(path, *records):
    path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")


@pytest.mark.asyncio
async def test_replay_returns_recorded_responses_in_order_and_parses_llm_response(tmp_path):
    first_messages = [Message(role="user", content="first")]
    second_messages = [Message(role="user", content="second")]
    path = tmp_path / "provider-attempts.jsonl"
    _write(path,
           _record("call-1", first_messages, LLMResponse(kind="final", content="one").model_dump()),
           _record("call-2", second_messages, LLMResponse(kind="clarification", content="which?").model_dump()))
    client = ProviderReplayClient(path)

    assert await client.complete(first_messages, []) == LLMResponse(kind="final", content="one")
    assert await client.complete(second_messages, []) == LLMResponse(kind="clarification", content="which?")
    assert client.exhausted


@pytest.mark.asyncio
async def test_replay_normalizes_ephemeral_tool_call_ids_and_references(tmp_path):
    recorded_messages = [
        Message(
            role="assistant",
            content="call",
            tool_calls=[ToolCall(call_id="recorded-call-id", name="lookup", arguments={"q": "same"})],
        ),
        Message(role="tool", content="result", tool_call_id="recorded-call-id"),
    ]
    replay_messages = [
        Message(
            role="assistant",
            content="call",
            tool_calls=[ToolCall(call_id="fresh-call-id", name="lookup", arguments={"q": "same"})],
        ),
        Message(role="tool", content="result", tool_call_id="fresh-call-id"),
    ]
    path = tmp_path / "provider-attempts.jsonl"
    _write(path, _record("call-1", recorded_messages, LLMResponse(kind="final", content="done").model_dump()))
    client = ProviderReplayClient(path)

    assert await client.complete(replay_messages, []) == LLMResponse(kind="final", content="done")


@pytest.mark.asyncio
async def test_replay_still_rejects_tool_call_name_or_arguments_mismatch(tmp_path):
    recorded = [Message(role="assistant", content="call", tool_calls=[
        ToolCall(call_id="recorded-id", name="lookup", arguments={"q": "expected"}),
    ])]
    actual = [Message(role="assistant", content="call", tool_calls=[
        ToolCall(call_id="new-id", name="lookup", arguments={"q": "wrong"}),
    ])]
    path = tmp_path / "provider-attempts.jsonl"
    _write(path, _record("call-1", recorded, LLMResponse(kind="final", content="done").model_dump()))
    client = ProviderReplayClient(path)

    with pytest.raises(ReplayError, match="request_mismatch"):
        await client.complete(actual, [])


def test_replay_normalization_restores_only_legacy_redacted_policy_boolean():
    from fixtures.provider_acceptance_replay import _canonicalize_tool_call_ids

    payload = {"messages": [{"role": "user", "content": json.dumps({
        "assistant_runtime_result": json.dumps({
            "tools": [{"constraints": {
                "requires_execution_authorization": "[REDACTED]",
                "api_key": "[REDACTED]",
            }}]
        })
    })}]}

    normalized = _canonicalize_tool_call_ids(payload)
    outer = json.loads(normalized["messages"][0]["content"])
    result = json.loads(outer["assistant_runtime_result"])
    constraints = result["tools"][0]["constraints"]
    assert constraints["requires_execution_authorization"] is True
    assert constraints["api_key"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_replay_rejects_exhaustion_wrong_order_and_missing_identity(tmp_path):
    messages = [Message(role="user", content="expected")]
    path = tmp_path / "attempts.jsonl"
    _write(path, _record("call-1", messages, LLMResponse(kind="final", content="ok").model_dump()))
    client = ProviderReplayClient(path)
    with pytest.raises(ReplayError, match="request_mismatch"):
        await client.complete([Message(role="user", content="wrong")], [])


@pytest.mark.asyncio
async def test_replay_mismatch_reports_only_differing_message_positions(tmp_path):
    expected = [Message(role="user", content="private expected text")]
    path = tmp_path / "attempts.jsonl"
    _write(path, _record("call-1", expected, LLMResponse(kind="final", content="ok").model_dump()))
    client = ProviderReplayClient(path)

    with pytest.raises(ReplayError) as error:
        await client.complete([Message(role="user", content="private actual text")], [])

    assert str(error.value).endswith("messages=0;extra_keys=-")
    assert "private" not in str(error.value)


@pytest.mark.asyncio
async def test_replay_mismatch_exposes_redacted_expected_and_actual_payloads(tmp_path):
    expected = [Message(role="user", content="expected")]
    path = tmp_path / "attempts.jsonl"
    _write(path, _record("call-1", expected, LLMResponse(kind="final", content="ok").model_dump()))
    client = ProviderReplayClient(path)

    with pytest.raises(ReplayError, match="request_mismatch") as caught:
        await client.complete(
            [Message(role="user", content='{"text":"actual","api_key":"private-key","authorization":"Bearer private-token"}')], [],
        )

    assert caught.value.call_id == "call-1"
    assert caught.value.expected_payload["messages"][0]["content"] == "expected"
    actual = caught.value.actual_payload["messages"][0]["content"]
    assert json.loads(actual) == {
        "text": "actual", "api_key": "[REDACTED]", "authorization": "[REDACTED]",
    }
    assert "private-token" not in json.dumps(caught.value.actual_payload)
    assert "private-key" not in json.dumps(caught.value.actual_payload)


@pytest.mark.asyncio
async def test_replay_reports_recorded_http_and_provider_errors_deterministically(tmp_path):
    messages = [Message(role="user", content="error")]
    path = tmp_path / "attempts.jsonl"
    _write(path, _record("http-call", messages, status=503),
           _record("provider-call", messages, error_type="TimeoutError"))
    client = ProviderReplayClient(path)
    with pytest.raises(ReplayError, match="recorded_http_error:503"):
        await client.complete(messages, [])
    with pytest.raises(ReplayError, match="recorded_provider_error:TimeoutError"):
        await client.complete(messages, [])


@pytest.mark.asyncio
async def test_replay_wrapper_records_recorded_error(tmp_path, monkeypatch):
    messages = [Message(role="user", content="error")]
    source = tmp_path / "provider-attempts.jsonl"
    _write(source, _record("call-1", messages, error_type="RuntimeError"))
    monkeypatch.setenv("RUNTIME_ACCEPTANCE_MAX_CALLS", "2")
    monkeypatch.setenv("RUNTIME_ACCEPTANCE_MODE", "replay")
    monkeypatch.setenv("RUNTIME_ACCEPTANCE_ROOT", str(tmp_path))

    provider = CappedProvider(ProviderReplayClient(source))
    with pytest.raises(ReplayError, match="recorded_provider_error:RuntimeError"):
        await provider.complete(messages, [])

    records = [json.loads(line) for line in (tmp_path / "replay-attempts.jsonl").read_text().splitlines()]
    assert records == [{
        "mode": "replay", "scenario_id": None, "call_index": 1,
        "status": "error", "error_type": "ReplayError",
        "error": "recorded_provider_error:RuntimeError",
    }]


def test_runtime_event_and_operation_observers_read_isolated_run_database(tmp_path, monkeypatch):
    import sqlite3

    import tests.fixtures.runtime_provider_acceptance as harness

    database = tmp_path / "agent.db"
    with sqlite3.connect(database) as db:
        db.executescript(
            "CREATE TABLE task_run_events (run_id TEXT, sequence INTEGER, event_type TEXT);"
            "CREATE TABLE execution_operations (run_id TEXT, logical_index INTEGER, tool_name TEXT, status TEXT);"
        )
        db.execute("INSERT INTO task_run_events VALUES ('run-1', 1, 'assistant.waiting')")
        db.execute("INSERT INTO task_run_events VALUES ('run-1', 2, 'execution.failed')")
        db.execute("INSERT INTO execution_operations VALUES ('run-1', 1, 'search_knowledge', 'succeeded')")
        db.execute("INSERT INTO execution_operations VALUES ('run-1', 2, 'read_file', 'failed')")
    assert harness.observed_events("run-1", database) == ["assistant.waiting", "execution.failed"]
    assert harness.observed_operations("run-1", database) == ["search_knowledge"]


def test_replay_creates_chrome_target_with_put_method():
    import inspect

    import tests.fixtures.runtime_provider_acceptance as harness

    source = inspect.getsource(harness.main)
    replay_branch = source.split("if replay_mode:", 1)[1].split("else:", 1)[0]
    assert "browser_session.put(" in replay_branch
    assert "browser_session.post(" not in replay_branch


@pytest.mark.asyncio
async def test_replay_never_constructs_http_client_and_summary_is_separate(tmp_path, monkeypatch):
    messages = [Message(role="user", content="offline")]
    source = tmp_path / "provider-attempts.jsonl"
    _write(source, _record("call-1", messages, LLMResponse(kind="final", content="offline").model_dump()))

    def forbidden(*args, **kwargs):
        raise AssertionError("HTTP client must not be constructed during replay")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    client = ProviderReplayClient(source)
    assert await client.complete(messages, []) == LLMResponse(kind="final", content="offline")
    summary_path = tmp_path / "replay" / "summary.json"
    write_replay_summary(summary_path, source_path=source, client=client, status="completed")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["mode"] == "replay"
    assert summary["real_provider_compatibility_claim"] is False
    assert summary["source_sha256"]
    assert source.exists()
    assert not (tmp_path / "summary.json").exists()


def test_replay_rejects_invalid_and_ambiguous_records(tmp_path):
    path = tmp_path / "attempts.jsonl"
    path.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(ReplayError, match="invalid_attempt_json"):
        ProviderReplayClient(path)
    path.write_text(json.dumps({"call_id": "call-1", "response": {}}), encoding="utf-8")
    with pytest.raises(ReplayError, match="missing_request_identity"):
        ProviderReplayClient(path)


def test_replay_module_does_not_import_compatible_provider_client():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, 'tests'); "
         "import fixtures.provider_acceptance_replay; "
         "assert 'app.llm.compatible_client' not in sys.modules"],
        cwd=__import__("pathlib").Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr