"""Provider wrapper shared by the isolated Runtime acceptance harness."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from app.llm.compatible_client import _REQUEST_CONTEXT, _REQUEST_CORRELATION
from app.observability.provider_attempt_recorder import ProviderAttemptRecorder
from app.errors import ProviderCallBudgetExceeded
from tests.fixtures.provider_acceptance_executor import count_recorded_calls


class CappedProvider:
    def _budget_blocked(self, root, scenario, *, scope, used, limit):
        correlation = _REQUEST_CORRELATION.get() or {}
        record = {
            "event_type": "budget.blocked", "reason_code": ProviderCallBudgetExceeded.code,
            "scope": scope, "used": used, "limit": limit,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "scenario_id": scenario.get("scenario_id"),
            **{key: correlation.get(key) for key in ("run_id", "step_id", "call_id")},
        }
        with (root / "budget-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
        raise ProviderCallBudgetExceeded()

    def __init__(self, client):
        self.client = client
        self.response_format_identity = getattr(client, "response_format_identity", None)
        self.calls = int(os.environ.get("RUNTIME_ACCEPTANCE_USED_CALLS", "0"))
        self.timeout = getattr(client, "timeout", None)
        self.is_replay = os.environ.get("RUNTIME_ACCEPTANCE_MODE") == "replay"

    async def complete(self, messages, tools, *, response_format=None):
        limit = int(os.environ["RUNTIME_ACCEPTANCE_MAX_CALLS"])
        root = Path(os.environ["RUNTIME_ACCEPTANCE_ROOT"]).resolve()
        scenario_path = root / "active-scenario.json"
        scenario = json.loads(scenario_path.read_text(encoding="utf-8")) if scenario_path.exists() else {}
        if self.calls >= limit:
            self._budget_blocked(root, scenario, scope="global", used=self.calls, limit=limit)
        scenario_id = scenario.get("scenario_id")
        scenario_calls = 0
        if scenario_id:
            attempts_path = root / "provider-attempts.jsonl"
            scenario_calls = count_recorded_calls(attempts_path, scenario_id)
            if scenario_calls >= int(scenario["request_budget"]):
                self._budget_blocked(root, scenario, scope="scenario", used=scenario_calls,
                                     limit=int(scenario["request_budget"]))
        self.calls += 1
        if self.is_replay:
            try:
                response = await self.client.complete(messages, tools, response_format=response_format)
            except Exception as exc:
                self.calls = self.client.calls
                with (root / "replay-attempts.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({
                        "mode": "replay", "scenario_id": scenario_id,
                        "call_index": self.client.calls, "status": "error",
                        "error_type": type(exc).__name__, "error": str(exc)[:500],
                    }, ensure_ascii=False) + "\n")
                if isinstance(exc, Exception) and hasattr(exc, "actual_payload"):
                    diagnostic = {
                        "mode": "replay",
                        "scenario_id": scenario_id,
                        "call_index": self.client.calls + 1,
                        "call_id": exc.call_id,
                        "error": str(exc)[:500],
                        "expected_payload": exc.expected_payload,
                        "actual_payload": exc.actual_payload,
                    }
                    with (root / "replay-mismatches.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(diagnostic, ensure_ascii=False) + "\n")
                raise
            finally:
                self.calls = self.client.calls
            with (root / "replay-attempts.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "mode": "replay", "scenario_id": scenario_id,
                    "call_index": self.client.calls, "status": "response",
                }, ensure_ascii=False) + "\n")
            return response

        correlation = _REQUEST_CORRELATION.get() or {}
        recorder = ProviderAttemptRecorder(
            root / "provider-attempts.jsonl", self.calls, correlation.get("run_id"),
            acceptance_id=os.environ.get("RUNTIME_ACCEPTANCE_ID"), scenario_id=scenario_id,
        )
        recorder.record["scenario_request_index"] = scenario_calls + 1 if scenario_id else None
        for key in ("acceptance_id", "scenario_id", "run_id", "call_id", "step_id", "operation_id"):
            if correlation.get(key) is not None:
                recorder.record[key] = correlation[key]
        recorder.record["correlation_missing"] = not bool(recorder.record.get("call_id"))
        token = _REQUEST_CONTEXT.set(recorder.observe)
        try:
            response = await self.client.complete(messages, tools, response_format=response_format)
            recorder.record["response_format"] = response_format
            recorder.record["response"] = response.model_dump(mode="json")
            return response
        except BaseException as exc:
            recorder.record["error_type"] = type(exc).__name__
            raise
        finally:
            _REQUEST_CONTEXT.reset(token)
            try:
                recorder.write()
            except Exception:
                from app.observability.provider_diagnostics import diagnostic_warning

                diagnostic_warning()