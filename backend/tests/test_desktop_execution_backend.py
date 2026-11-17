import pytest
from pydantic import ValidationError

from app.execution.desktop_backend import (
    BackendCapabilities,
    BackendOperationResult,
    ChangeCollectionResult,
    ExecutionBackendUnavailable,
    ExecutionHandle,
    ExecutionObservation,
    ExecutionPolicy,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    UnavailableExecutionBackend,
    WorkspaceSnapshot,
)


class ContractFakeExecutionBackend:
    def __init__(self):
        self.handles = {}
        self.results = {}
        self.fail_close = False

    async def describe_capabilities(self):
        return BackendCapabilities(
            backend_id="contract-fake", platform="test", command_execution=True,
            network_access=False, workspace_writeback=False, supports_cancel=True,
            supports_inspect=True,
        )

    async def prepare(self, run_id, workspace_snapshot, policy):
        handle = ExecutionHandle(handle_id=f"handle-{run_id}", run_id=run_id)
        self.handles[handle.handle_id] = (run_id, workspace_snapshot, policy)
        return BackendOperationResult(status=ExecutionStatus.COMPLETED, handle=handle)

    async def execute(self, request):
        existing = self.results.get(request.invocation_id)
        if existing is not None:
            return existing
        prepared = self.handles.get(request.handle_id)
        if prepared is None:
            result = ExecutionResult(status=ExecutionStatus.REJECTED, error_code="handle_unknown")
        else:
            _, _, policy = prepared
            if request.timeout_seconds > policy.timeout_limit_seconds:
                result = ExecutionResult(status=ExecutionStatus.REJECTED, error_code="timeout_limit_exceeded")
            elif not set(request.environment).issubset(policy.environment_allowlist):
                result = ExecutionResult(status=ExecutionStatus.REJECTED, error_code="environment_rejected")
            elif request.argv[0] == "timeout":
                result = ExecutionResult(status=ExecutionStatus.TIMED_OUT, error_code="timeout")
            elif request.argv[0] in {"crash", "running"}:
                result = ExecutionResult(status=ExecutionStatus.UNKNOWN, error_code="process_state_unknown")
            else:
                output = request.argv[-1].encode("utf-8")
                truncated = len(output) > policy.output_limit_bytes
                output = output[:policy.output_limit_bytes]
                result = ExecutionResult(status=ExecutionStatus.COMPLETED, exit_code=0,
                    stdout=output.decode("utf-8", errors="ignore"), output_truncated=truncated)
        self.results[request.invocation_id] = result
        return result

    async def cancel(self, handle_id, invocation_id):
        if handle_id not in self.handles or invocation_id not in self.results:
            return BackendOperationResult(status=ExecutionStatus.REJECTED, error_code="invocation_unknown")
        self.results[invocation_id] = ExecutionResult(status=ExecutionStatus.CANCELLED,
            error_code="cancelled")
        return BackendOperationResult(status=ExecutionStatus.COMPLETED)

    async def inspect(self, handle_id, invocation_id):
        result = self.results.get(invocation_id)
        if result is None or handle_id not in self.handles:
            return ExecutionObservation(status=ExecutionStatus.UNKNOWN,
                observed_status=ExecutionStatus.UNKNOWN, error_code="process_state_unknown")
        return ExecutionObservation(status=ExecutionStatus.COMPLETED, observed_status=result.status)

    async def collect_changes(self, handle_id):
        if handle_id not in self.handles:
            return ChangeCollectionResult(status=ExecutionStatus.REJECTED, error_code="handle_unknown")
        return ChangeCollectionResult(status=ExecutionStatus.COMPLETED,
            changeset_id=f"changes-{handle_id}")

    async def close(self, handle_id):
        if self.fail_close:
            return BackendOperationResult(status=ExecutionStatus.FAILED, error_code="cleanup_failed")
        self.handles.pop(handle_id, None)
        return BackendOperationResult(status=ExecutionStatus.COMPLETED)


@pytest.mark.asyncio
async def test_unavailable_backend_reports_no_command_or_network_capability():
    capabilities = await UnavailableExecutionBackend().describe_capabilities()

    assert isinstance(capabilities, BackendCapabilities)
    assert capabilities.protocol_version == 1
    assert capabilities.command_execution is False
    assert capabilities.network_access is False
    assert capabilities.workspace_writeback is False


@pytest.mark.asyncio
async def test_unavailable_backend_rejects_prepare_without_creating_host_execution():
    backend = UnavailableExecutionBackend()

    result = await backend.prepare(
        run_id="run-1",
        workspace_snapshot=WorkspaceSnapshot(snapshot_id="snapshot-1", digest="a" * 64),
        policy=ExecutionPolicy(),
    )

    assert result.status == ExecutionStatus.REJECTED
    assert result.error_code == ExecutionBackendUnavailable.code
    assert result.handle is None


@pytest.mark.asyncio
async def test_unavailable_backend_rejects_every_operation_with_stable_status():
    backend = UnavailableExecutionBackend()
    request = ExecutionRequest(
        handle_id="handle-1",
        invocation_id="invocation-1",
        argv=("python", "-c", "print('must not run')"),
        cwd=".",
        timeout_seconds=5,
    )

    execution = await backend.execute(request)
    cancelled = await backend.cancel("handle-1", "invocation-1")
    observation = await backend.inspect("handle-1", "invocation-1")
    changes = await backend.collect_changes("handle-1")
    cleanup = await backend.close("handle-1")

    assert execution.status == ExecutionStatus.REJECTED
    assert cancelled.status == ExecutionStatus.REJECTED
    assert observation.status == ExecutionStatus.REJECTED
    assert changes.status == ExecutionStatus.REJECTED
    assert cleanup.status == ExecutionStatus.REJECTED
    assert all(item.error_code == ExecutionBackendUnavailable.code for item in
               (execution, cancelled, observation, changes, cleanup))


def test_execution_request_requires_structured_argv_and_workspace_relative_cwd():
    from app.assistant.tools.commands import RunCommandArguments

    with pytest.raises(ValidationError):
        ExecutionRequest(
            handle_id="handle-1",
            invocation_id="invocation-1",
            command="python -c 'print(1)'",
            cwd=".",
            timeout_seconds=5,
        )

    with pytest.raises(ValidationError):
        ExecutionRequest(
            handle_id="handle-1",
            invocation_id="invocation-1",
            argv=("python", "-V"),
            cwd="../outside",
            timeout_seconds=5,
        )

    with pytest.raises(ValidationError):
        ExecutionRequest(
            handle_id="handle-1", invocation_id="large-argv", argv=("x" * 65_537,),
            cwd=".", timeout_seconds=5,
        )

    with pytest.raises(ValidationError):
        ExecutionRequest(
            handle_id="handle-1", invocation_id="large-env", argv=("python", "-V"), cwd=".",
            environment={"LANG": "x" * 65_537}, timeout_seconds=5,
        )

    with pytest.raises(ValidationError):
        ExecutionPolicy(environment_allowlist=("LANG", "LANG"))

    with pytest.raises(ValidationError):
        RunCommandArguments.model_validate({"argv": ["x" * 65_537], "cwd": "."})

    with pytest.raises(ValidationError):
        RunCommandArguments.model_validate({"command": "python -V", "cwd": "."})

    with pytest.raises(ValidationError):
        RunCommandArguments.model_validate({"argv": ["x" * 65_537], "cwd": "."})

    with pytest.raises(ValidationError):
        RunCommandArguments.model_validate({"command": "python -V", "cwd": "."})


def test_runtime_composition_injects_the_execution_backend_protocol(tmp_path):
    from app.dependencies import build_app_dependencies
    from app.settings import Settings

    dependencies = build_app_dependencies(Settings(
        _env_file=None,
        config_file=None,
        database_path=str(tmp_path / "runtime.db"),
        code_task_workspace_root=tmp_path / "workspaces",
        code_task_artifact_root=tmp_path / "artifacts",
        docs_root=tmp_path / "docs",
    ))

    assert isinstance(dependencies.execution_backend, UnavailableExecutionBackend)
    assert dependencies.task_service.assistant.execution_backend is dependencies.execution_backend


@pytest.mark.asyncio
async def test_runtime_catalog_explains_that_command_execution_is_unavailable(tmp_path):
    from app.dependencies import build_app_dependencies
    from app.settings import Settings
    from app.tools.base import ToolContext

    dependencies = build_app_dependencies(Settings(
        _env_file=None,
        config_file=None,
        database_path=str(tmp_path / "runtime.db"),
        code_task_workspace_root=tmp_path / "workspaces",
        code_task_artifact_root=tmp_path / "artifacts",
        docs_root=tmp_path / "docs",
    ))
    assistant = dependencies.task_service.assistant
    command = next(item for item in assistant.registry.catalog(query="run_command")
                  if item["name"] == "run_command")
    assert command["available"] is False
    assert command["reason"] == ExecutionBackendUnavailable.code

    result = await assistant.registry.get("run_command").execute(
        {"argv": ["python", "-V"], "cwd": ".", "timeout_seconds": 5},
        ToolContext(session_id="session-1", run_id="run-1"),
    )
    assert result.success is False
    assert result.data["error"]["category"] == "unavailable"
    assert result.data["error"]["code"] == ExecutionBackendUnavailable.code


@pytest.mark.asyncio
async def test_fake_backend_contract_enforces_idempotency_environment_and_output_budget():
    backend = ContractFakeExecutionBackend()
    snapshot = WorkspaceSnapshot(snapshot_id="snapshot-1", digest="a" * 64)
    policy = ExecutionPolicy(output_limit_bytes=4, environment_allowlist=("LANG",))
    prepared = await backend.prepare("run-1", snapshot, policy)
    assert prepared.status == ExecutionStatus.COMPLETED
    assert prepared.handle == ExecutionHandle(handle_id="handle-run-1", run_id="run-1")

    request = ExecutionRequest(
        handle_id="handle-run-1", invocation_id="invoke-1", argv=("echo", "123456"),
        cwd=".", environment={"LANG": "C"}, timeout_seconds=5,
    )
    first = await backend.execute(request)
    repeated = await backend.execute(request)
    assert first == repeated
    assert first.status == ExecutionStatus.COMPLETED
    assert first.stdout == "1234"
    assert first.output_truncated is True

    denied = await backend.execute(request.model_copy(update={
        "invocation_id": "invoke-denied", "environment": {"SECRET_VALUE": "never pass"},
    }))
    assert denied.status == ExecutionStatus.REJECTED
    assert denied.error_code == "environment_rejected"
    too_slow = await backend.execute(request.model_copy(update={
        "invocation_id": "invoke-time-limit", "timeout_seconds": 301,
    }))
    assert too_slow.status == ExecutionStatus.REJECTED
    assert too_slow.error_code == "timeout_limit_exceeded"


@pytest.mark.asyncio
async def test_fake_backend_contract_reports_timeout_cancel_unknown_changes_and_cleanup():
    backend = ContractFakeExecutionBackend()
    handle_id = "handle-run-2"
    await backend.prepare("run-2", WorkspaceSnapshot(snapshot_id="snapshot-2", digest="b" * 64),
        ExecutionPolicy())

    timed_out = await backend.execute(ExecutionRequest(
        handle_id=handle_id, invocation_id="invoke-timeout", argv=("timeout",), cwd=".", timeout_seconds=1,
    ))
    assert timed_out.status == ExecutionStatus.TIMED_OUT

    running = await backend.execute(ExecutionRequest(
        handle_id=handle_id, invocation_id="invoke-cancel", argv=("running", "process"), cwd=".", timeout_seconds=5,
    ))
    assert running.status == ExecutionStatus.UNKNOWN
    cancelled = await backend.cancel(handle_id, "invoke-cancel")
    observed = await backend.inspect(handle_id, "invoke-cancel")
    assert cancelled.status == ExecutionStatus.COMPLETED
    assert observed.observed_status == ExecutionStatus.CANCELLED

    unknown = await backend.execute(ExecutionRequest(
        handle_id=handle_id, invocation_id="invoke-crash", argv=("crash",), cwd=".", timeout_seconds=5,
    ))
    assert unknown.status == ExecutionStatus.UNKNOWN
    assert (await backend.inspect(handle_id, "missing")).observed_status == ExecutionStatus.UNKNOWN
    assert (await backend.collect_changes(handle_id)).changeset_id == "changes-handle-run-2"

    backend.fail_close = True
    assert (await backend.close(handle_id)).error_code == "cleanup_failed"
    backend.fail_close = False
    assert (await backend.close(handle_id)).status == ExecutionStatus.COMPLETED
    assert (await backend.close(handle_id)).status == ExecutionStatus.COMPLETED
