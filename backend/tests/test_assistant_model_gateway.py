import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from app.agent.models import LLMResponse, Message
from app.execution.ports import LeaseExecutionPort
from app.execution.recovery import RecoveryConflict, RecoveryObservation, classify_recovery
from app.execution.state_machine import StaleExecutionOwner
from test_execution_recovery import setup_repository
from test_assistant_persistence import assistant_snapshot


class Provider:
    def __init__(self):
        self.calls = 0
        self.error = False
        self.block = False
        self.cancelled = False

    async def complete(self, messages, tools):
        self.calls += 1
        if self.error:
            raise RuntimeError("provider failed")
        if self.block:
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True
        return LLMResponse(kind="final", content="answer")


async def gateway_fixture(tmp_path, **limits):
    from app.assistant.model_gateway import RunModelGateway
    repo, run, lease, clock = await setup_repository(tmp_path)
    await repo.save_snapshot(lease, assistant_snapshot(run.id, **limits))
    provider = Provider()
    port = LeaseExecutionPort(repo, lease, None)
    gateway = RunModelGateway(provider, port, config_digest="config", timeout_seconds=10)
    return gateway, provider, repo, run, lease, clock


MESSAGES = [Message(role="user", content="hello")]


async def test_shared_budget_top_response_reuse_and_input_conflict(tmp_path):
    gateway, provider, repo, run, _, _ = await gateway_fixture(tmp_path)
    response = await gateway.complete(MESSAGES, [], context={"step_id": "decision-1"})
    assert await gateway.complete(MESSAGES, [], context={"step_id": "decision-1"}) == response
    with pytest.raises(RecoveryConflict, match="model_input_conflict"):
        await gateway.complete([Message(role="user", content="changed")], [],
                               context={"step_id": "decision-1"})
    client = gateway.scoped_client(str(uuid4()), "outline", "attempt-1")
    await client.complete(MESSAGES, [])
    with pytest.raises(RuntimeError, match="model_budget_exhausted"):
        await client.complete(MESSAGES, [])
    assert provider.calls == 2
    assert (await repo.get_snapshot(run.id)).reserved_model_calls == 2
    ledger = await repo.get_model_calls(run.id)
    assert [row["scope"] for row in ledger] == ["top", "domain"]
    assert ledger[1]["phase"] == "outline"


async def test_failed_provider_and_new_domain_attempt_do_not_refund_or_replay(tmp_path):
    gateway, provider, repo, run, _, _ = await gateway_fixture(tmp_path)
    operation_id = str(uuid4())
    provider.error = True
    with pytest.raises(RuntimeError, match="provider failed"):
        await gateway.scoped_client(operation_id, "outline", "a1").complete(MESSAGES, [])
    provider.error = False
    await gateway.scoped_client(operation_id, "outline", "a2").complete(MESSAGES, [])
    assert provider.calls == 2
    ledger = await repo.get_model_calls(run.id)
    assert [row["status"] for row in ledger] == ["failed", "committed"]
    assert [row["attempt_id"] for row in ledger] == ["a1", "a2"]


async def test_config_conflict_and_stale_lease_reject_before_provider(tmp_path):
    gateway, provider, _, _, _, clock = await gateway_fixture(tmp_path)
    gateway.config_digest = "changed"
    with pytest.raises(RecoveryConflict, match="model_config_conflict"):
        await gateway.complete(MESSAGES, [], context={"step_id": "one"})
    gateway.config_digest = "config"
    clock.value += timedelta(seconds=31)
    with pytest.raises(StaleExecutionOwner):
        await gateway.complete(MESSAGES, [], context={"step_id": "one"})
    assert provider.calls == 0


async def test_remaining_time_caps_provider_and_timeout_is_counted(tmp_path):
    gateway, provider, repo, run, _, _ = await gateway_fixture(tmp_path,
        elapsed_upper_bound_seconds=19.98)
    provider.block = True
    with pytest.raises(TimeoutError):
        await gateway.complete(MESSAGES, [], context={"step_id": "one"})
    assert provider.cancelled
    snap = await repo.get_snapshot(run.id)
    assert snap.reserved_model_calls == 1
    assert snap.elapsed_upper_bound_seconds >= 19.99


async def test_cancel_propagates_and_late_commit_is_fenced(tmp_path):
    gateway, provider, repo, run, lease, _ = await gateway_fixture(tmp_path)
    provider.block = True
    cancel = asyncio.Event()
    task = asyncio.create_task(gateway.complete(MESSAGES, [],
        context={"step_id": "one", "cancel_event": cancel}))
    while provider.calls == 0:
        await asyncio.sleep(0.001)
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.cancelled
    assert (await repo.get_snapshot(run.id)).reserved_model_calls == 1
    await repo.request_cancel(run.id)
    ledger = await repo.get_model_calls(run.id)
    with pytest.raises(StaleExecutionOwner):
        await repo.commit_model_call(lease, ledger[0]["id"], LLMResponse(kind="final", content="late"))


async def test_lost_response_crash_settles_reserved_time_and_keeps_count(tmp_path):
    gateway, provider, repo, run, lease, clock = await gateway_fixture(tmp_path,
        budget_limits={"max_model_calls": 2, "max_execution_seconds": 60.0})
    reserved = await repo.reserve_model_call(lease, scope="top", step_id="lost",
        operation_id=None, phase="decision", attempt_id="top", input_digest="input",
        config_digest="config", timeout_seconds=4)
    assert reserved["status"] == "reserved"
    clock.value += timedelta(seconds=31)
    await repo.expire_and_classify(lease.attempt_id)
    snap = await repo.get_snapshot(run.id)
    assert snap.reserved_model_calls == 1
    assert snap.elapsed_upper_bound_seconds >= 4
    assert (await repo.get_model_calls(run.id))[0]["status"] == "unknown"
    candidate = await repo.get_recovery_candidate(run.id)
    decision = classify_recovery(candidate.snapshot, candidate.operation,
                                 RecoveryObservation(policy_digest="policy"))
    assert decision.action == "retry"
    await repo.apply_recovery(candidate, decision)
    new_lease = await repo.claim(run.id, "new", lease_seconds=30)
    gateway.port.lease = new_lease
    await gateway.complete(MESSAGES, [], context={"step_id": "new"})
    assert provider.calls == 1
    assert (await repo.get_snapshot(run.id)).reserved_model_calls == 2


async def test_restart_and_wait_preserve_committed_response_and_budget(tmp_path):
    from app.assistant.model_gateway import RunModelGateway
    from app.execution.repository import ExecutionRepository
    gateway, provider, repo, run, lease, clock = await gateway_fixture(tmp_path)
    await gateway.complete(MESSAGES, [], context={"step_id": "saved"})
    elapsed_before_wait = (await repo.get_snapshot(run.id)).elapsed_upper_bound_seconds
    await repo.release(lease)
    clock.value += timedelta(days=1)
    restarted = ExecutionRepository(repo.path, clock=clock)
    await restarted.init()
    new_lease = await restarted.claim(run.id, "restart", lease_seconds=30)
    recovered = RunModelGateway(provider, LeaseExecutionPort(restarted, new_lease, None),
                                config_digest="config", timeout_seconds=10)
    await recovered.complete(MESSAGES, [], context={"step_id": "saved"})
    assert provider.calls == 1
    snap = await restarted.get_snapshot(run.id)
    assert snap.reserved_model_calls == 1
    assert snap.elapsed_upper_bound_seconds == elapsed_before_wait


async def test_old_snapshot_cannot_clear_ledger_counters(tmp_path):
    gateway, _, repo, run, lease, _ = await gateway_fixture(tmp_path)
    old = await repo.get_snapshot(run.id)
    await gateway.complete(MESSAGES, [], context={"step_id": "one"})
    await repo.save_snapshot(lease, old)
    assert (await repo.get_snapshot(run.id)).reserved_model_calls == 1


async def test_zero_time_and_unknown_write_block_new_generation(tmp_path):
    gateway, provider, repo, run, lease, _ = await gateway_fixture(tmp_path,
        elapsed_upper_bound_seconds=20.0)
    with pytest.raises(TimeoutError, match="run_time_exhausted"):
        await gateway.complete(MESSAGES, [], context={"step_id": "one"})
    assert provider.calls == 0
    assert await repo.get_model_calls(run.id) == []


async def test_domain_generation_blocked_by_inflight_write(tmp_path):
    from app.execution.models import OperationInput
    gateway, provider, repo, run, lease, _ = await gateway_fixture(tmp_path)
    snap = await repo.get_snapshot(run.id)
    op = OperationInput(id=uuid4(), logical_index=1, kind="tool", tool_name="save",
                        input_payload={}, recovery_class="artifact")
    await repo.prepare_operation(lease, op, snap)
    await repo.start_step(lease, op.id, snap)
    with pytest.raises(RecoveryConflict, match="unknown_side_effect"):
        await gateway.scoped_client(str(op.id), "draft", "new").complete(MESSAGES, [])
    assert provider.calls == 0


async def test_domain_client_inherits_cancellation_and_parent_deadline(tmp_path):
    gateway, provider, repo, run, _, _ = await gateway_fixture(tmp_path)
    gateway.context = {"remaining_seconds": 0.02, "cancel_event": asyncio.Event()}
    provider.block = True
    with pytest.raises(TimeoutError):
        await gateway.scoped_client(str(uuid4()), "draft", "a1").complete(MESSAGES, [])
    assert provider.cancelled
    assert (await repo.get_model_calls(run.id))[0]["timeout_seconds"] == 0.02


async def test_commit_loss_is_counted_and_response_not_reused(tmp_path, monkeypatch):
    gateway, provider, repo, run, _, _ = await gateway_fixture(tmp_path)
    original = repo.commit_model_call

    async def lose_response(lease, call_id, response=None, **kwargs):
        if response is not None:
            raise RuntimeError("response lost")
        return await original(lease, call_id, response, **kwargs)

    monkeypatch.setattr(repo, "commit_model_call", lose_response)
    with pytest.raises(RuntimeError, match="response lost"):
        await gateway.complete(MESSAGES, [], context={"step_id": "one"})
    monkeypatch.setattr(repo, "commit_model_call", original)
    await gateway.complete(MESSAGES, [], context={"step_id": "one"})
    assert provider.calls == 2
    assert (await repo.get_snapshot(run.id)).reserved_model_calls == 2


async def test_active_time_between_calls_and_wait_is_not_reset(tmp_path):
    gateway, _, repo, run, lease, clock = await gateway_fixture(tmp_path)
    await gateway.complete(MESSAGES, [], context={"step_id": "one"})
    before = (await repo.get_snapshot(run.id)).elapsed_upper_bound_seconds
    clock.value += timedelta(seconds=3)
    await repo.release(lease)
    assert (await repo.get_snapshot(run.id)).elapsed_upper_bound_seconds >= before + 3


async def test_concurrent_reservations_cannot_overrun_budget(tmp_path):
    gateway, provider, repo, run, _, _ = await gateway_fixture(tmp_path)
    clients = [gateway.scoped_client(str(uuid4()), "outline", str(i)) for i in range(3)]
    results = await asyncio.gather(*(client.complete(MESSAGES, []) for client in clients),
                                   return_exceptions=True)
    assert sum(isinstance(result, RuntimeError) for result in results) == 1
    assert provider.calls == 2
    assert (await repo.get_snapshot(run.id)).reserved_model_calls == 2