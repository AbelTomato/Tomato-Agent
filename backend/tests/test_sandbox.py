import asyncio

import pytest
from pydantic import ValidationError

from app.agent.policies import CapabilityProfile
from app.agent.sandbox import (
    LocalSandboxExecutor,
    SandboxRequest,
    SandboxResult,
)


def profile(**overrides) -> CapabilityProfile:
    values = {"timeout_seconds": 0.05, "max_output_chars": 5}
    values.update(overrides)
    return CapabilityProfile(**values)


def request(**overrides) -> SandboxRequest:
    values = {
        "tool_name": "fake_read",
        "arguments": {"value": "ok"},
        "input_refs": ("evidence-1",),
        "request_id": "request-1",
    }
    values.update(overrides)
    return SandboxRequest(**values)


@pytest.mark.asyncio
async def test_local_executor_fails_closed_without_backend():
    result = await LocalSandboxExecutor().execute(request(), profile())

    assert result.status == "rejected"
    assert result.error_code == "backend_unavailable"
    assert result.exit_code is None


def test_request_does_not_accept_model_submitted_policy():
    with pytest.raises(ValidationError):
        SandboxRequest(
            tool_name="fake_read",
            arguments={},
            input_refs=(),
            request_id="request-1",
            policy={"allow_process": True},
        )


@pytest.mark.asyncio
async def test_profile_validation_fails_closed_for_unsafe_capabilities():
    with pytest.raises(ValidationError):
        profile(allow_network=True)
    with pytest.raises(ValidationError):
        profile(allowed_paths=("/",))
    with pytest.raises(ValidationError):
        profile(credential_names=("API_KEY",))

    result = await LocalSandboxExecutor(lambda request, profile: {"stdout": "unsafe"}).execute(
        request(), profile()
    )
    assert result.status == "completed"
    assert result.stdout == "unsaf"


@pytest.mark.asyncio
async def test_executor_maps_success_and_truncates_output():
    async def backend(request, profile):
        return {"exit_code": 0, "stdout": "123456", "stderr": "warning"}

    result = await LocalSandboxExecutor(backend).execute(request(), profile())

    assert result == SandboxResult(
        status="completed",
        exit_code=0,
        stdout="12345",
        stderr="",
        output_truncated=True,
        error_code=None,
    )


@pytest.mark.asyncio
async def test_executor_maps_timeout_backend_failure_and_bad_profile():
    async def slow_backend(request, profile):
        await asyncio.sleep(1)

    timed_out = await LocalSandboxExecutor(slow_backend).execute(request(), profile())
    assert timed_out.status == "timed_out"
    assert timed_out.error_code == "timeout"

    async def failing_backend(request, profile):
        raise RuntimeError("secret host details")

    failed = await LocalSandboxExecutor(failing_backend).execute(request(), profile())
    assert failed.status == "failed"
    assert failed.error_code == "execution_failed"
    assert "secret" not in failed.stderr

    rejected = await LocalSandboxExecutor(slow_backend).execute(request(), object())
    assert rejected.status == "rejected"
    assert rejected.error_code == "invalid_profile"


@pytest.mark.asyncio
async def test_executor_maps_cancellation_without_touching_business_state():
    started = asyncio.Event()

    async def cancellable_backend(request, profile):
        started.set()
        await asyncio.sleep(1)

    task = asyncio.create_task(
        LocalSandboxExecutor(cancellable_backend).execute(request(), profile())
    )
    await started.wait()
    task.cancel()
    result = await task

    assert result.status == "cancelled"
    assert result.error_code == "cancelled"
    assert not hasattr(result, "task_status")