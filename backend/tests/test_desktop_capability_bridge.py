import asyncio
import json

import pytest
from pydantic import ValidationError

from app.desktop.capability_bridge import DesktopCapabilityBridge, DesktopBridgeError
from app.desktop.capability_bridge import DesktopBridgeRequest


@pytest.mark.asyncio
async def test_bridge_sends_authenticated_versioned_request_and_matches_response():
    observed = {}

    async def serve(reader, writer):
        observed.update(json.loads(await reader.readline()))
        response = {
            "protocol_version": 1,
            "instance_id": "desktop-1",
            "request_id": observed["request_id"],
            "status": "completed",
            "result": {"entries": ["README.md"]},
        }
        writer.write(json.dumps(response).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        result = await DesktopCapabilityBridge(
            port=port, token="t" * 64, instance_id="desktop-1"
        ).call("list_entries", {"relative_path": "."}, grant="run-grant",
               session_id="session-1", run_id="run-1", project_id="project-1")

    assert result == {"entries": ["README.md"]}
    assert observed["protocol_version"] == 1
    assert observed["instance_id"] == "desktop-1"
    assert observed["session_id"] == "session-1"
    assert observed["run_id"] == "run-1"
    assert observed["project_id"] == "project-1"
    assert abs(observed["timestamp_ms"] - int(__import__("time").time() * 1000)) < 5000
    assert observed["authorization"] == "Bearer " + "t" * 64
    assert observed["operation"] == "list_entries"
    assert observed["grant"] == "run-grant"
    assert observed["nonce"]


@pytest.mark.asyncio
async def test_bridge_rejects_mismatched_instance_response():
    async def serve(reader, writer):
        request = json.loads(await reader.readline())
        writer.write(json.dumps({
            "protocol_version": 1,
            "instance_id": "other-instance",
            "request_id": request["request_id"],
            "status": "completed",
            "result": {},
        }).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        with pytest.raises(DesktopBridgeError, match="bridge_instance_mismatch"):
            await DesktopCapabilityBridge(
                port=port, token="t" * 64, instance_id="desktop-1"
            ).call("read_file", {"relative_path": "README.md"}, grant="run-grant",
                   session_id="session-1", run_id="run-1", project_id="project-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_change", "expected_code"),
    [
        ({"protocol_version": 2}, "bridge_protocol_mismatch"),
        ({"request_id": "another-request"}, "bridge_request_mismatch"),
        ({"status": "rejected", "error_code": "grant_expired", "result": None}, "grant_expired"),
    ],
)
async def test_bridge_rejects_protocol_request_and_operation_mismatches(response_change, expected_code):
    async def serve(reader, writer):
        request = json.loads(await reader.readline())
        response = {
            "protocol_version": 1,
            "instance_id": "desktop-1",
            "request_id": request["request_id"],
            "status": "completed",
            "result": {},
            "error_code": None,
            **response_change,
        }
        writer.write(json.dumps(response).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        with pytest.raises(DesktopBridgeError, match=expected_code):
            await DesktopCapabilityBridge(
                port=port, token="t" * 64, instance_id="desktop-1"
            ).call("read_file", {"relative_path": "README.md"}, grant="run-grant",
                   session_id="session-1", run_id="run-1", project_id="project-1")


def test_bridge_request_requires_session_run_and_project_identity():
    with pytest.raises(ValidationError):
        DesktopBridgeRequest(
            instance_id="desktop-1",
            session_id="session-1",
            run_id="",
            project_id="project-1",
            request_id="request-1",
            nonce="nonce-1",
            timestamp_ms=1,
            authorization="Bearer " + "t" * 64,
            operation="read_file",
            grant="run-grant",
            payload={"relative_path": "README.md"},
        )
