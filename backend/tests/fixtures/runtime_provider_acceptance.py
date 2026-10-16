"""Opt-in local browser acceptance for the configured real Provider."""

import argparse
import asyncio
import base64
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

from unified_browser_acceptance import Browser
try:
    from fixtures.provider_acceptance_executor import AcceptanceScenario, execute_scenarios
    from fixtures.provider_acceptance_state import ScenarioRecord, ScenarioStore
except ModuleNotFoundError:
    from provider_acceptance_executor import AcceptanceScenario, execute_scenarios
    from provider_acceptance_state import ScenarioRecord, ScenarioStore


BACKEND = Path(__file__).parents[2]
ROOT = BACKEND / "data" / "runtime-provider-acceptance"
MAX_RUNS = 4
MAX_CALLS = 16
RUN_SECONDS = 600
LLM_TIMEOUT_SECONDS = 600


def free_port(start):
    for port in range(start, start + 100):
        with socket.socket() as listener:
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free frontend port in configured range")


async def wait_scenario_task(api, run_id, timeout_seconds):
    async with asyncio.timeout(timeout_seconds):
        while True:
            task = (await api.get(f"/api/tasks/{run_id}")).json()
            if task["status"] in {"completed", "failed", "cancelled", "timed_out"}:
                return task
            await asyncio.sleep(1)


async def new_session(browser):
    previous = await browser.evaluate("localStorage.getItem('tomato-agent-session-id')")
    await browser.click("新建会话")
    await browser.wait(
        "!!localStorage.getItem('tomato-agent-session-id') && "
        "localStorage.getItem('tomato-agent-session-id') !== " + json.dumps(previous), 20)
    await browser.wait(
        "document.querySelectorAll('.task-card').length===0 && "
        "!!document.querySelector('.composer textarea') && "
        "!document.querySelector('.composer textarea').disabled", 20)
    return await browser.evaluate("localStorage.getItem('tomato-agent-session-id')")


async def bind_workspace(browser, workspace_id):
    await browser.evaluate("document.querySelector('.workspace-picker').open=true")
    await browser.wait("!!document.querySelector('.workspace-picker select') && "
                       "!document.querySelector('.workspace-picker select').disabled", 20)
    await browser.evaluate(
        "(() => {const e=document.querySelector('.workspace-picker select');"
        "e.value=" + json.dumps(workspace_id) + ";"
        "e.dispatchEvent(new Event('change',{bubbles:true}));})()")
    await browser.wait("document.querySelector('.workspace-picker select').value === "
                       + json.dumps(workspace_id) + " && "
                       "!document.querySelector('.workspace-picker select').disabled", 20)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-real-provider", action="store_true")
    parser.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--single-run", action="store_true")
    parser.add_argument("--max-runs", type=int, default=MAX_RUNS)
    parser.add_argument("--max-requests", type=int, default=MAX_CALLS)
    args = parser.parse_args()
    if not args.run_real_provider:
        parser.error("real Provider requests require --run-real-provider")
    if not 1 <= args.max_runs <= MAX_RUNS or not 1 <= args.max_requests <= MAX_CALLS:
        parser.error(f"budget must be 1-{MAX_RUNS} runs and 1-{MAX_CALLS} requests")
    run_root = (ROOT / args.run_id).resolve()
    if not run_root.is_relative_to(ROOT.resolve()) or (run_root.exists() != args.resume):
        parser.error("use a new run-id, or pass --resume for an existing isolated directory")
    if not (BACKEND / ".env").is_file():
        parser.error("backend/.env is required; credentials will not be printed")
    try:
        cdp_response = httpx.get(args.cdp_url.rstrip("/") + "/json/list", timeout=3)
        cdp_response.raise_for_status()
        cdp_pages = cdp_response.json()
        if not any(item.get("type") == "page" for item in cdp_pages):
            raise ValueError("no debuggable Chrome page")
    except (httpx.HTTPError, ValueError) as exc:
        parser.error(f"Chrome CDP is unavailable at {args.cdp_url}: {type(exc).__name__}")
    run_root.mkdir(parents=True, exist_ok=args.resume)
    used_calls = 0
    attempts_path = run_root / "provider-attempts.jsonl"
    if attempts_path.exists():
        used_calls = sum(1 for line in attempts_path.read_text(encoding="utf-8").splitlines() if line.strip())
    if used_calls >= args.max_requests:
        parser.error("existing run has exhausted the Provider call budget")
    if args.resume and (run_root / "agent.db").exists():
        with sqlite3.connect(run_root / "agent.db") as existing_db:
            existing_statuses = existing_db.execute(
                "SELECT status FROM task_runs WHERE task_type='assistant' ORDER BY created_at"
            ).fetchall()
        if existing_statuses and any(status not in {"waiting", "queued", "running"} for (status,) in existing_statuses):
            parser.error("existing acceptance Run is terminal; refusing to resubmit or mutate it")
    env = os.environ.copy()
    env.update({
        "RUNTIME_ACCEPTANCE_ROOT": str(run_root),
        "RUNTIME_ACCEPTANCE_ID": args.run_id,
        "RUNTIME_ACCEPTANCE_RUN_SECONDS": str(RUN_SECONDS),
        "RUNTIME_ACCEPTANCE_LLM_TIMEOUT_SECONDS": str(LLM_TIMEOUT_SECONDS),
        "RUNTIME_ACCEPTANCE_STREAM": "1" if args.stream else "",
        "RUNTIME_ACCEPTANCE_MAX_RUNS": str(args.max_runs),
        "RUNTIME_ACCEPTANCE_MAX_CALLS": str(args.max_requests),
        "RUNTIME_ACCEPTANCE_USED_CALLS": str(used_calls),
        "RUNTIME_ACCEPTANCE_RESUME": "1" if args.resume else "",
    })
    frontend_env = os.environ.copy()
    frontend_env["VITE_API_BASE_URL"] = "http://127.0.0.1:8037"
    frontend_port = free_port(5197)
    frontend_url = f"http://127.0.0.1:{frontend_port}"
    env["RUNTIME_ACCEPTANCE_FRONTEND_ORIGIN"] = frontend_url
    server_log = (run_root / "server.log").open("w", encoding="utf-8")
    frontend_log = (run_root / "frontend.log").open("w", encoding="utf-8")
    if not args.resume and (run_root / "agent.db").exists():
        parser.error("refusing to reuse an existing acceptance database")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tests.fixtures.runtime_provider_acceptance_server:app",
         "--host", "127.0.0.1", "--port", "8037"], cwd=BACKEND, env=env,
        stdout=server_log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    frontend = subprocess.Popen(
        ["pnpm", "dev", "--host", "127.0.0.1", "--port", str(frontend_port), "--strictPort"],
        cwd=BACKEND.parent / "frontend", env=frontend_env,
        stdout=frontend_log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    browser = Browser()
    browser_target_id = None
    cdp_events_path = run_root / "cdp-events.jsonl"

    def observe(event, **details):
        with cdp_events_path.open("a", encoding="utf-8") as events:
            events.write(json.dumps({"event": event, **details}, ensure_ascii=False) + "\n")
    summary_path = run_root / "summary.json"
    summary = (json.loads(summary_path.read_text(encoding="utf-8")) if args.resume and summary_path.exists()
               else {"status": "running", "run_id": args.run_id, "runs": [], "checks": {}})
    summary["status"] = "running"
    try:
        async with httpx.AsyncClient(timeout=3) as health:
            for url in ("http://127.0.0.1:8037/health", frontend_url):
                async with asyncio.timeout(90):
                    while True:
                        try:
                            if (await health.get(url)).status_code < 500:
                                break
                        except httpx.HTTPError:
                            if server.poll() is not None and url.endswith(":8037/health"):
                                raise RuntimeError("acceptance_server_exited_before_health_check")
                            pass
                        await asyncio.sleep(0.5)
        async with httpx.AsyncClient() as cdp:
            created = await cdp.put(args.cdp_url.rstrip("/") + "/json/new?" + frontend_url + "/")
            created.raise_for_status()
            browser_target_id = created.json()["id"]
        observe("target_created", target_id=browser_target_id, url=frontend_url + "/")
        await browser.connect(args.cdp_url, target_id=browser_target_id)
        observe("target_connected", target_id=browser.target_id, url=browser.target_url)
        await browser.wait(
            "document.readyState==='complete' && location.origin===" + json.dumps(frontend_url), 30)
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8037", timeout=10) as api:
            scenario_store = ScenarioStore(run_root / "scenarios.json", acceptance_id=args.run_id)
            interrupted = scenario_store.recover_running()
            if interrupted:
                summary["scenarios"] = json.loads((run_root / "scenarios.json").read_text(encoding="utf-8"))["scenarios"]
                raise RuntimeError("interrupted_scenario_requires_new_acceptance_run")
            with sqlite3.connect(run_root / "agent.db") as db:
                existing = db.execute("SELECT id,session_id,status FROM task_runs WHERE task_type='assistant' ORDER BY created_at").fetchall()
            initial_run_count = len(existing)
            if args.resume and existing:
                raise RuntimeError("resume_with_existing_runs_is_not_supported_by_scenario_executor")

            def provider_calls():
                if not attempts_path.exists():
                    return 0
                return sum(1 for line in attempts_path.read_text(encoding="utf-8").splitlines() if line.strip())

            def scenario_record(scenario_id, timeout, budget, terminal, events=(), operations=()):
                return ScenarioRecord(
                    acceptance_id=args.run_id, scenario_id=scenario_id, timeout_seconds=timeout,
                    request_budget=min(budget, args.max_requests), expected_terminal=terminal,
                    expected_events=list(events), expected_operations=list(operations),
                    diagnostic_files={
                        "events": "cdp-events.jsonl", "provider_attempts": "provider-attempts.jsonl",
                        "ledger_and_operations": "agent.db", "server_log": "server.log",
                        "frontend_log": "frontend.log", "ui": "browser.png",
                    },
                )

            async def clarification_scenario(context):
                request_start = provider_calls()
                session = await new_session(browser)
                await browser.submit("assistant", "请先问我一个澄清问题，确认想了解的主题和目标；收到回答前不要使用任何工具。")
                with sqlite3.connect(run_root / "agent.db") as db:
                    run_id = db.execute("SELECT id FROM task_runs WHERE task_type='assistant' ORDER BY created_at DESC LIMIT 1").fetchone()[0]
                clarify = (await api.get(f"/api/tasks/{run_id}")).json()
                if clarify["status"] != "waiting" or (clarify.get("pending_action") or {}).get("action_type") != "clarification":
                    return {"run_id": str(run_id), "terminal": clarify["status"], "events": [], "operations": [],
                            "request_count": provider_calls() - request_start}
                summary["checks"]["clarification_wait"] = True
                await browser.wait("!!document.querySelector('.pending-action-panel textarea')", 25)
                await browser.input(".pending-action-panel textarea", "解释 Redis SETEX 的 TTL，优先使用本地知识证据。")
                await browser.click("提交补充说明")
                result = await wait_scenario_task(api, run_id, context.record.timeout_seconds)
                await browser.command("Page.reload")
                await browser.wait("document.querySelector('.assistant-result')?.innerText.length>0", 25)
                return {"run_id": str(run_id), "terminal": result["status"],
                        "events": ["clarification.waiting", "run.completed"], "operations": [],
                        "request_count": provider_calls() - request_start, "refresh": True}

            async def cross_capability_scenario(context):
                request_start = provider_calls()
                session = await new_session(browser)
                workspaces = (await api.get("/api/workspaces")).json()["workspaces"]
                fixture_workspace = next(item for item in workspaces if item["repo_path"].endswith("/project"))
                await bind_workspace(browser, fixture_workspace["workspace_id"])
                await browser.submit("assistant", "结合本地知识中的 Redis SETEX 资料与已注册项目文件 cache.py，说明该实现的 TTL，并给出来源。只读，不要执行测试。")
                with sqlite3.connect(run_root / "agent.db") as db:
                    run_id, workspace_id = db.execute(
                        "SELECT id,workspace_id FROM task_runs WHERE task_type='assistant' AND session_id=? ORDER BY created_at DESC LIMIT 1", (session,)
                    ).fetchone()
                if workspace_id != fixture_workspace["workspace_id"]:
                    raise RuntimeError("cross_capability_workspace_mismatch")
                result = await wait_scenario_task(api, run_id, context.record.timeout_seconds)
                with sqlite3.connect(run_root / "agent.db") as db:
                    tools = [row[0] for row in db.execute(
                        "SELECT tool_name FROM execution_operations WHERE run_id=? AND status='succeeded'", (run_id,))]
                await browser.command("Page.reload")
                await browser.wait("document.querySelectorAll('.task-card').length>0", 25)
                return {"run_id": str(run_id), "terminal": result["status"], "events": ["run.completed"],
                        "operations": tools, "request_count": provider_calls() - request_start, "refresh": True}

            async def terminal_refresh_scenario(context):
                request_start = provider_calls()
                await new_session(browser)
                await browser.submit("assistant", "请用两句话解释 Redis 持久化的主要权衡。")
                with sqlite3.connect(run_root / "agent.db") as db:
                    run_id = db.execute("SELECT id FROM task_runs WHERE task_type='assistant' ORDER BY created_at DESC LIMIT 1").fetchone()[0]
                result = await wait_scenario_task(api, run_id, context.record.timeout_seconds)
                await browser.command("Page.reload")
                await browser.wait("document.querySelectorAll('.task-card').length>0", 25)
                terminal_event = "run." + result["status"]
                return {"run_id": str(run_id), "terminal": result["status"], "events": [terminal_event],
                        "operations": [], "request_count": provider_calls() - request_start, "refresh": True}

            definitions = [
                AcceptanceScenario(scenario_record("clarification_continue", RUN_SECONDS, 6, "completed",
                    ["clarification.waiting", "run.completed"]), clarification_scenario),
                AcceptanceScenario(scenario_record("cross_capability", RUN_SECONDS, 6, "completed",
                    ["run.completed"], ["read_file", "search_knowledge"]), cross_capability_scenario),
                AcceptanceScenario(scenario_record("terminal_refresh", RUN_SECONDS, 4, "completed",
                    ["run.completed"]), terminal_refresh_scenario),
            ]
            if args.single_run:
                definitions = definitions[:1]
            scenario_results = await execute_scenarios(
                definitions, scenario_store, max_runs=args.max_runs, max_requests=args.max_requests,
                prior_runs=initial_run_count, prior_requests=used_calls,
                request_observer=lambda scenario_id, record: (run_root / "active-scenario.json").write_text(
                    json.dumps({"scenario_id": scenario_id, "request_budget": record.request_budget}),
                    encoding="utf-8",
                ),
            )
            summary["scenarios"] = scenario_results
            summary["runs"] = [{"run_id": item["run_id"],
                                "final_status": (item.get("actual_observations") or {}).get("terminal"),
                                "successful_tools": (item.get("actual_observations") or {}).get("operations", [])}
                               for item in scenario_results.values() if item.get("run_id")]
            all_passed = len(scenario_results) == len(definitions) and all(
                item["status"] == "passed" for item in scenario_results.values())
            summary["status"] = ("completed_single_run" if args.single_run else "passed") if all_passed else "failed"
            if not all_passed:
                failed = next((item for item in scenario_results.values() if item["status"] != "passed"), None)
                raise RuntimeError("provider_acceptance_scenario_failed:" + str((failed or {}).get("error_category")))

    except BaseException as exc:
        summary["status"] = "failed"
        summary["error_type"] = type(exc).__name__
        summary["error"] = str(exc)[:500]
        observe("run_error", error_type=type(exc).__name__, error=str(exc)[:500],
                target_id=getattr(browser, "target_id", None), target_url=getattr(browser, "target_url", None))
        raise
    finally:
        try:
            image = await browser.command("Page.captureScreenshot", format="png")
            (run_root / "browser.png").write_bytes(base64.b64decode(image["data"]))
        except BaseException:
            pass
        (run_root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        if getattr(browser, "ws", None):
            await browser.ws.close()
        if browser_target_id:
            try:
                async with httpx.AsyncClient() as cdp:
                    closed = await cdp.put(args.cdp_url.rstrip("/") + f"/json/close/{browser_target_id}")
                    observe("target_closed", target_id=browser_target_id, status=closed.status_code)
            except Exception as close_error:
                observe("target_close_error", target_id=browser_target_id, error_type=type(close_error).__name__)
        for process in (frontend, server):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        server_log.close()
        frontend_log.close()
    print(json.dumps({"status": summary["status"], "report": str(run_root / "summary.json"),
                      "run_ids": [item["run_id"] for item in summary["runs"]]}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())