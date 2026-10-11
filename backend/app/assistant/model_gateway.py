"""Run-local model calls with durable reservations and lease fencing."""

import asyncio
import math

from app.agent.models import LLMResponse
from app.execution.ports import canonical_digest
from app.execution.state_machine import StaleExecutionOwner


class RunModelGateway:
    def __init__(self, client, port, *, config_digest: str, timeout_seconds: float):
        if not config_digest or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("config digest and positive finite timeout required")
        self.client = client
        self.port = port
        self.config_digest = config_digest
        self.timeout_seconds = timeout_seconds
        self.context = {}

    def scoped_client(self, operation_id: str, phase: str, attempt_id: str):
        if not operation_id or not phase or not attempt_id:
            raise ValueError("domain scope requires operation, phase and attempt")
        return _ScopedClient(self, operation_id, phase, attempt_id)

    async def complete(self, messages, tools, context):
        return await self._complete(messages, tools, scope="top", step_id=context["step_id"],
            operation_id=None, phase="decision", attempt_id="top", context=context)

    async def _complete(self, messages, tools, *, scope, step_id, operation_id, phase, attempt_id, context):
        context = {**self.context, **context}
        cancel_event = context.get("cancel_event")
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError
        timeout = min(self.timeout_seconds, context.get("remaining_seconds", self.timeout_seconds))
        if context.get("deadline") is not None:
            timeout = min(timeout, context["deadline"] - asyncio.get_running_loop().time())
        if timeout <= 0:
            raise TimeoutError("parent_time_exhausted")
        provider_timeout = getattr(self.client, "timeout", None)
        if provider_timeout is not None and getattr(provider_timeout, "read", None) is not None:
            timeout = min(timeout, provider_timeout.read)
        digest = canonical_digest({"messages": [item.model_dump(mode="json") for item in messages],
                                   "tools": [item.model_dump(mode="json") for item in tools]})
        row = await self.port.reserve_model_call(
            scope=scope, step_id=step_id, operation_id=operation_id, phase=phase,
            attempt_id=attempt_id, input_digest=digest, config_digest=self.config_digest,
            timeout_seconds=timeout)
        if row["status"] == "committed":
            return LLMResponse.model_validate_json(row["response"])
        request = asyncio.create_task(self.client.complete(messages, tools))
        cancellation = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        try:
            async with asyncio.timeout(row["timeout_seconds"]):
                if cancellation is not None:
                    done, _ = await asyncio.wait({request, cancellation}, return_when=asyncio.FIRST_COMPLETED)
                    if cancellation in done:
                        raise asyncio.CancelledError
                response = await request
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError
            await self.port.commit_model_call(row["id"], response)
            return response
        except BaseException as exc:
            request.cancel()
            code = "cancelled" if isinstance(exc, asyncio.CancelledError) else (
                "timeout" if isinstance(exc, TimeoutError) else "provider_failed")
            try:
                await self.port.commit_model_call(row["id"], error_code=code)
            except StaleExecutionOwner:
                pass
            raise
        finally:
            if cancellation is not None:
                cancellation.cancel()
            await asyncio.gather(request, *([cancellation] if cancellation is not None else []),
                                 return_exceptions=True)


class _ScopedClient:
    def __init__(self, gateway, operation_id, phase, attempt_id):
        self.gateway = gateway
        self.operation_id = operation_id
        self.phase = phase
        self.attempt_id = attempt_id
        self.context = {}

    async def complete(self, messages, tools):
        return await self.gateway._complete(messages, tools, scope="domain", step_id=None,
            operation_id=self.operation_id, phase=self.phase, attempt_id=self.attempt_id,
            context=self.context)