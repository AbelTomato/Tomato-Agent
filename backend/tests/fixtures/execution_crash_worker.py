"""Fake-only subprocess worker; stdin/stdout are deterministic fault barriers."""

import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
from uuid import UUID, uuid4

from app.agent.context import ContextManager
from app.agent.harness import Harness
from app.agent.models import LLMResponse, ToolCall
from app.execution.models import OperationInput, StepOutcome
from app.execution.ports import LeaseExecutionPort
from app.execution.repository import ExecutionRepository
from app.execution.state_machine import StaleExecutionOwner
from app.tools.base import ToolResult
from app.tools.registry import ToolRegistry
from test_execution_recovery import Clock, snapshot
from test_harness import FakeTool, RecordingLLM, ScriptedStrategy, budget, policy, state


def emit(payload):
    print(json.dumps(payload), flush=True)


def read():
    return json.loads(sys.stdin.readline())


class Faults:
    def __init__(self, target):
        self.target = target

    def hit(self, point):
        if self.target == point:
            emit({"fault": point})
            if read()["action"] == "exit":
                os._exit(73)
            raise RuntimeError("invalid fault command")


class CrashPort(LeaseExecutionPort):
    def __init__(self, repository, lease, faults, recovery_class):
        super().__init__(repository, lease, "workspace")
        self.faults = faults
        self.recovery_class = recovery_class

    async def describe_operation(self, logical_index, tool_name, arguments):
        return OperationInput(id=uuid4(), logical_index=logical_index, kind="tool",
            tool_name=tool_name, input_payload=arguments, recovery_class=self.recovery_class)

    async def commit_model_response(self, response, snapshot, operation=None):
        saved = await super().commit_model_response(response, snapshot, operation)
        self.faults.hit("response_after")
        return saved

    async def commit_step(self, step_id, result, snapshot):
        self.repository.transaction_fault = "commit_before"
        saved = await super().commit_step(step_id, result, snapshot)
        self.repository.transaction_fault = None
        self.faults.hit("commit_after")
        return saved


class CrashRepository(ExecutionRepository):
    transaction_fault = None

    async def _commit(self, db):
        if self.transaction_fault:
            self.faults.hit(self.transaction_fault)
        await super()._commit(db)


class CrashLLM(RecordingLLM):
    def __init__(self, responses, faults):
        super().__init__(responses)
        self.faults = faults

    async def complete(self, messages, tools):
        self.faults.hit("model_before")
        return await super().complete(messages, tools)


class SideEffectTool(FakeTool):
    def __init__(self, path, faults, recovery_class):
        self.path = path
        self.faults = faults
        self.recovery_class = recovery_class

    async def execute(self, arguments, context):
        # A durable marker represents a fake file/Sandbox side effect, not cleanup.
        with self.path.open("a") as stream:
            stream.write(self.recovery_class + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.faults.hit("effect_after")
        return ToolResult(success=True, data={"value": arguments["value"] + 1})


async def claim_worker(repository, run_id, clock):
    emit({"ready": True})
    read()
    try:
        lease = await repository.claim(run_id, f"worker-{os.getpid()}", lease_seconds=30)
    except RuntimeError as exc:
        if str(exc) != "run is already claimed":
            raise
        emit({"claimed": False})
        return
    saved = snapshot(run_id)
    operation = OperationInput(id=uuid4(), logical_index=0, kind="tool", tool_name="read",
                              input_payload={}, recovery_class="read_only")
    saved = saved.model_copy(update={"pending_operation_id": operation.id})
    await repository.prepare_operation(lease, operation, saved)
    step = await repository.start_step(lease, operation.id, saved)
    emit({"claimed": True})
    command = read()
    clock.value = datetime.fromisoformat(command["now"])
    rejected = []
    actions = {
        "heartbeat": lambda: repository.heartbeat(lease, lease_seconds=30),
        "snapshot": lambda: repository.save_snapshot(lease, saved),
        "model": lambda: repository.commit_model_response(lease, LLMResponse(kind="final", content="old"), saved),
        "step": lambda: repository.commit_step(lease, step.id, StepOutcome(success=True, result_payload={}), saved),
        "finish": lambda: repository.finish(lease, "completed", snapshot=saved),
    }
    for name, action in actions.items():
        try:
            await action()
        except StaleExecutionOwner:
            rejected.append(name)
    emit({"rejected": rejected})


async def main():
    path, run_id, mode, fault, recovery_class = sys.argv[1:]
    path, run_id = Path(path), UUID(run_id)
    clock = Clock()
    if mode == "resume":
        clock.value += timedelta(seconds=31)
    repository = CrashRepository(path, clock=clock)
    repository.faults = Faults(fault)
    if mode == "claim":
        await claim_worker(repository, run_id, clock)
        return
    faults = repository.faults
    lease = await repository.claim(run_id, f"worker-{os.getpid()}", lease_seconds=30)
    saved = await repository.get_snapshot(run_id)
    pending = saved is not None and (saved.phase != "ready_model" or saved.logical_index > 0)
    decisions = [] if saved and saved.phase == "ready_finish" else (
        [{"kind": "respond", "output": "done"}] if pending else [
            {"kind": "call_tool", "tool_name": "add", "arguments": {"value": 1}},
            {"kind": "respond", "output": "done"},
        ])
    responses = [LLMResponse(kind="final", content="done")] if pending else [
        LLMResponse(kind="tool_call", tool_call=ToolCall(call_id="stable", name="add", arguments={"value": 1})),
        LLMResponse(kind="final", content="done"),
    ]
    result = await Harness(ContextManager(token_counter=len)).run(
        state(), ScriptedStrategy(decisions), llm=CrashLLM(responses, faults),
        tools=ToolRegistry([SideEffectTool(path.with_suffix(".effects"), faults, recovery_class)]),
        policy=policy(timeout_seconds=1), budget=budget(max_duration_seconds=30, max_loops=5),
        execution_port=CrashPort(repository, lease, faults, recovery_class), resume_snapshot=saved,
    )
    repository.transaction_fault = "finish_before"
    saved = await repository.get_snapshot(run_id)
    status = "completed" if result.status == "completed" else "failed"
    await repository.finish(lease, status, snapshot=saved, event_type="execution." + status)
    faults.hit("finish_after")
    emit({"status": status, "reason": result.stop_reason})


if __name__ == "__main__":
    asyncio.run(main())