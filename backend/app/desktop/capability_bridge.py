import asyncio
import json
import time
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError


DESKTOP_BRIDGE_PROTOCOL_VERSION = 1
MAX_BRIDGE_MESSAGE_BYTES = 1_048_576


class DesktopBridgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    protocol_version: Literal[1] = DESKTOP_BRIDGE_PROTOCOL_VERSION
    instance_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    request_id: str = Field(min_length=1, max_length=200)
    nonce: str = Field(min_length=1, max_length=200)
    timestamp_ms: int = Field(ge=0)
    authorization: str = Field(min_length=1, max_length=256)
    operation: Literal[
        "list_entries",
        "search_text",
        "read_file",
        "apply_changeset",
        "inspect_commit",
        "revoke_run",
    ]
    grant: str = Field(min_length=1, max_length=512)
    payload: dict


class DesktopBridgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    protocol_version: Literal[1]
    instance_id: str = Field(min_length=1, max_length=200)
    request_id: str = Field(min_length=1, max_length=200)
    status: Literal["completed", "rejected"]
    result: dict | None = None
    error_code: str | None = None


class DesktopBridgeError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class WorkspaceBroker(Protocol):
    async def list_entries(self, grant: str, relative_path: str) -> dict: ...

    async def search_text(
        self, grant: str, relative_path: str, query: str, *, max_results: int
    ) -> dict: ...

    async def read_file(self, grant: str, relative_path: str, *, max_bytes: int) -> dict: ...

    async def apply_changeset(self, approval: str, changeset: dict) -> dict: ...

    async def inspect_commit(self, commit_id: str) -> dict: ...

    async def revoke_run(self, grant: str, *, session_id: str, run_id: str, project_id: str) -> dict: ...


class DesktopCapabilityBridge:
    """Private loopback client for desktop-owned project capabilities."""

    def __init__(self, *, port: int, token: str, instance_id: str, timeout_seconds: float = 5):
        if not 1 <= port <= 65535 or len(token) < 32 or not instance_id:
            raise ValueError("invalid_desktop_bridge_configuration")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("invalid_desktop_bridge_timeout")
        self.port = port
        self.token = token
        self.instance_id = instance_id
        self.timeout_seconds = timeout_seconds

    async def call(
        self,
        operation: str,
        payload: dict,
        *,
        grant: str,
        session_id: str,
        run_id: str,
        project_id: str,
    ) -> dict:
        request = DesktopBridgeRequest(
            instance_id=self.instance_id,
            session_id=session_id,
            run_id=run_id,
            project_id=project_id,
            request_id=str(uuid4()),
            nonce=str(uuid4()),
            timestamp_ms=time.time_ns() // 1_000_000,
            authorization=f"Bearer {self.token}",
            operation=operation,
            grant=grant,
            payload=payload,
        )
        encoded = request.model_dump_json().encode("utf-8") + b"\n"
        if len(encoded) > MAX_BRIDGE_MESSAGE_BYTES:
            raise DesktopBridgeError("bridge_request_too_large")

        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    "127.0.0.1", self.port, limit=MAX_BRIDGE_MESSAGE_BYTES + 1
                ),
                timeout=self.timeout_seconds,
            )
            writer.write(encoded)
            await asyncio.wait_for(writer.drain(), timeout=self.timeout_seconds)
            response_line = await asyncio.wait_for(
                reader.readline(), timeout=self.timeout_seconds
            )
            if not response_line or len(response_line) > MAX_BRIDGE_MESSAGE_BYTES:
                raise DesktopBridgeError("bridge_invalid_response")
            response_payload = json.loads(response_line)
        except DesktopBridgeError:
            raise
        except (OSError, TimeoutError, ValueError, UnicodeDecodeError):
            raise DesktopBridgeError("bridge_unavailable") from None
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

        if not isinstance(response_payload, dict):
            raise DesktopBridgeError("bridge_invalid_response")
        if response_payload.get("protocol_version") != DESKTOP_BRIDGE_PROTOCOL_VERSION:
            raise DesktopBridgeError("bridge_protocol_mismatch")
        try:
            response = DesktopBridgeResponse.model_validate(response_payload)
        except ValidationError:
            raise DesktopBridgeError("bridge_invalid_response") from None
        if response.instance_id != self.instance_id:
            raise DesktopBridgeError("bridge_instance_mismatch")
        if response.request_id != request.request_id:
            raise DesktopBridgeError("bridge_request_mismatch")
        if response.status != "completed" or response.result is None:
            raise DesktopBridgeError(response.error_code or "bridge_rejected")
        return response.result


def encode_bridge_response(
    *, instance_id: str, request_id: str, result: dict | None = None,
    error_code: str | None = None,
) -> bytes:
    status = "completed" if error_code is None else "rejected"
    response = DesktopBridgeResponse(
        protocol_version=DESKTOP_BRIDGE_PROTOCOL_VERSION,
        instance_id=instance_id,
        request_id=request_id,
        status=status,
        result=result,
        error_code=error_code,
    )
    encoded = response.model_dump_json().encode("utf-8") + b"\n"
    if len(encoded) > MAX_BRIDGE_MESSAGE_BYTES:
        raise DesktopBridgeError("bridge_response_too_large")
    return encoded
