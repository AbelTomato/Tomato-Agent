import json

import httpx
import pytest

from app.agent.models import Message
from app.llm.compatible_client import OpenAICompatibleClient, _REQUEST_CONTEXT
from app.observability.provider_attempt_recorder import ProviderAttemptRecorder


def rows(client):
    return [json.loads(line) for line in (client.diagnostics_dir / "provider-attempts.jsonl").read_text().splitlines()]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("status", [400, 401, 429, 502, 504])
async def test_http_failure_records_context_without_blame_or_credentials(tmp_path, stream, status):
    def handler(request):
        return httpx.Response(status, text="failure secret-key", headers={
            "x-request-id": "request-42", "retry-after": "2", "set-cookie": "private-cookie"})

    client = OpenAICompatibleClient(api_key="secret-key", model="m", stream=stream,
        base_url="https://user:pass@example.test/v1?api_key=hidden", diagnostics_root=tmp_path,
        transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        await client.complete([Message(role="user", content="hello")], [])
    row = rows(client)[0]
    diagnostic = row["diagnostic"]
    assert row["http_status"] == status
    assert row["raw_response_body"] == "failure [REDACTED]"
    assert diagnostic["error_category"] == f"http_{status}"
    assert diagnostic["root_cause"] == "undetermined"
    assert diagnostic["endpoint"] == "https://example.test/v1"
    assert diagnostic["response_headers"]["x-request-id"] == "request-42"
    assert diagnostic["elapsed_ms"] >= 0
    assert diagnostic["request_shape"]["request_bytes"] > 0
    assert diagnostic["request_shape"]["roles"] == ["user"]
    text = "".join(path.read_text() for path in client.diagnostics_dir.iterdir())
    for secret in ("secret-key", "private-cookie", "user:pass", "hidden"):
        assert secret not in text
    assert "http_" + str(status) in (client.diagnostics_dir / "provider-error-report.md").read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout])
async def test_transport_error_keeps_original_exception_and_payload(tmp_path, exception):
    def handler(request):
        raise exception("sensitive exception", request=request)

    client = OpenAICompatibleClient(api_key="key", model="m", diagnostics_root=tmp_path,
        transport=httpx.MockTransport(handler))
    with pytest.raises(exception):
        await client.complete([], [])
    row = rows(client)[0]
    assert row["http_status"] is None
    assert row["diagnostic"]["error_category"] == "transport_" + exception.__name__
    assert row["diagnostic"]["exception_chain"] == [exception.__name__]
    assert "sensitive exception" not in json.dumps(row)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream,body", [(False, "not-json"), (True, "data: not-json\n\n")])
async def test_protocol_error_preserves_http_200_and_body(tmp_path, stream, body):
    client = OpenAICompatibleClient(api_key="key", model="m", stream=stream, diagnostics_root=tmp_path,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)))
    with pytest.raises(RuntimeError):
        await client.complete([], [])
    row = rows(client)[0]
    assert row["http_status"] == 200
    assert "not-json" in row["raw_response_body"]
    assert row["diagnostic"]["error_category"] == "response_protocol"


class BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise httpx.ReadError("private network error")


@pytest.mark.asyncio
async def test_stream_disconnect_preserves_partial_response(tmp_path):
    client = OpenAICompatibleClient(api_key="key", model="m", stream=True, diagnostics_root=tmp_path,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=BrokenStream())))
    with pytest.raises(httpx.ReadError):
        await client.complete([], [])
    row = rows(client)[0]
    assert row["http_status"] == 200
    assert "partial" in row["raw_response_body"]
    assert row["diagnostic"]["error_category"] == "transport_ReadError"
    assert row["diagnostic"]["response_complete"] is False
    assert row["diagnostic"]["first_event_ms"] is not None


@pytest.mark.asyncio
async def test_non_stream_disconnect_preserves_headers_and_partial_body(tmp_path):
    client = OpenAICompatibleClient(api_key="key", model="m", diagnostics_root=tmp_path,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=BrokenStream(),
            headers={"x-request-id": "partial-response"})))
    with pytest.raises(httpx.ReadError):
        await client.complete([], [])
    row = rows(client)[0]
    assert "partial" in row["raw_response_body"]
    assert row["diagnostic"]["response_headers"]["x-request-id"] == "partial-response"
    assert row["diagnostic"]["response_complete"] is False


@pytest.mark.asyncio
async def test_acceptance_observer_reuses_evidence_and_report(tmp_path):
    recorder = ProviderAttemptRecorder(tmp_path / "provider-attempts.jsonl", 1, "run-1")
    client = OpenAICompatibleClient(api_key="key", model="m", diagnostics_root=tmp_path / "unused",
        transport=httpx.MockTransport(lambda request: httpx.Response(502, text="bad gateway")))
    token = _REQUEST_CONTEXT.set(recorder.observe)
    try:
        with pytest.raises(RuntimeError):
            await client.complete([], [])
    finally:
        _REQUEST_CONTEXT.reset(token)
    recorder.write()
    assert not client.diagnostics_dir.exists()
    assert recorder.record["diagnostic"]["error_category"] == "http_502"
    assert (tmp_path / "provider-error-report.md").exists()


@pytest.mark.asyncio
async def test_diagnostic_write_failure_does_not_replace_original_result(tmp_path, monkeypatch):
    def fail(self):
        raise OSError("private file error")

    monkeypatch.setattr(ProviderAttemptRecorder, "write", fail)
    client = OpenAICompatibleClient(api_key="key", model="m", diagnostics_root=tmp_path,
        transport=httpx.MockTransport(lambda request: httpx.Response(502, text="bad gateway")))
    with pytest.raises(RuntimeError, match="HTTP 502"):
        await client.complete([], [])


@pytest.mark.asyncio
async def test_report_links_previous_success_and_config_opt_in(tmp_path):
    from app.dependencies import create_llm_client
    from app.settings import Settings

    config = Settings(config_file="", llm_api_key="key", llm_diagnostics_enabled=True,
                      llm_diagnostics_root=tmp_path)
    client = create_llm_client(config)
    assert client.diagnostics_dir.parent == tmp_path
    assert create_llm_client(Settings(config_file="", llm_api_key="key")).diagnostics_dir is None
    responses = iter([httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}),
                      httpx.Response(502, text="bad gateway")])
    client.transport = httpx.MockTransport(lambda request: next(responses))
    await client.complete([], [])
    with pytest.raises(RuntimeError):
        await client.complete([Message(role="user", content="next")], [])
    recorded = rows(client)
    failure = json.loads((client.diagnostics_dir / "provider-errors.jsonl").read_text())
    assert failure["previous_success_call_id"] == recorded[0]["call_id"]
    assert failure["message_count_delta"] == 1
    assert failure["attempt_line"] == 2
    assert recorded[1]["attempt"] == 2
    assert failure["changed_payload_fields"] == ["messages"]


@pytest.mark.asyncio
async def test_diagnostic_failure_does_not_break_success_and_is_disabled_by_default(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    client = OpenAICompatibleClient(api_key="key", model="m", diagnostics_root=blocker,
        transport=httpx.MockTransport(lambda request: httpx.Response(200,
            json={"choices": [{"message": {"content": "ok"}}]})))
    assert (await client.complete([], [])).content == "ok"
    default = OpenAICompatibleClient(api_key="key", model="m", transport=client.transport)
    assert (await default.complete([], [])).content == "ok"
    assert default.diagnostics_dir is None


def test_error_groups_and_tool_pairing():
    from app.observability.provider_diagnostics import error_group, request_shape

    assert error_group("http_401") == "authentication_permission"
    assert error_group("http_429") == "rate_limit"
    assert error_group("http_408") == "http_timeout"
    assert error_group("http_502") == "http_server_error"
    assert error_group("http_422") == "http_request_error"
    assert request_shape({"messages": [{"role": "tool", "tool_call_id": "missing"}]}, 12)["tool_pairing_issues"]
    assert not request_shape({"messages": [
        {"role": "assistant", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a"}]}, 12)["tool_pairing_issues"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_response_http_version_survives_reconstruction(tmp_path, stream):
    body = 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n' if stream else '{"choices":[{"message":{"content":"ok"}}]}'
    client = OpenAICompatibleClient(api_key="key", model="m", stream=stream, diagnostics_root=tmp_path,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body,
            extensions={"http_version": b"HTTP/2"})))
    await client.complete([], [])
    assert rows(client)[0]["diagnostic"]["http_version"] == "HTTP/2"


@pytest.mark.asyncio
async def test_cancellation_is_recorded_and_propagates(tmp_path):
    import asyncio

    def handler(request):
        raise asyncio.CancelledError()

    client = OpenAICompatibleClient(api_key="key", model="m", diagnostics_root=tmp_path,
        transport=httpx.MockTransport(handler))
    with pytest.raises(asyncio.CancelledError):
        await client.complete([], [])
    assert rows(client)[0]["diagnostic"]["error_category"] == "local_cancelled"


def test_diagnostic_toml_settings(tmp_path):
    from app.settings import Settings

    config = tmp_path / "config.toml"
    config.write_text('[providers]\nllm_diagnostics_enabled = true\nllm_diagnostics_root = "data/provider-diagnostics"\n')
    settings = Settings(config_file=config)
    assert settings.llm_diagnostics_enabled is True
    assert settings.llm_diagnostics_root.is_absolute()