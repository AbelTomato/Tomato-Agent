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


async def wait_task(api, run_id):
    async with asyncio.timeout(RUN_SECONDS + 15):
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
            with sqlite3.connect(run_root / "agent.db") as db:
                existing = db.execute("SELECT id,session_id,status FROM task_runs WHERE task_type='assistant' ORDER BY created_at").fetchall()
            if existing and args.resume:
                run_id, session_id, status = existing[0]
                clarify = (await api.get(f"/api/tasks/{run_id}")).json()
                session = str(session_id)
                observe("session_restore_start", session_id=session, run_id=run_id,
                        task_status=clarify["status"], pending_action=(clarify.get("pending_action") or {}).get("action_type"))
                if clarify["status"] == "waiting":
                    await browser.evaluate("localStorage.setItem('tomato-agent-session-id', " + json.dumps(session) + ")")
                    await browser.command("Page.reload")
                    observe("page_reloaded", target_id=browser.target_id)
                    await browser.wait("!!document.querySelector('.conversation-shell')", 25)
                    await browser.wait("!!document.querySelector('.pending-action-panel textarea')", 35)
                else:
                    await browser.wait("!!document.querySelector('.conversation-shell')", 25)
                observe("conversation_shell_ready", target_id=browser.target_id)
                if not summary["runs"]:
                    summary["runs"].append({"run_id": run_id, "initial_status": status})
                if clarify["status"] == "waiting":
                    summary["runs"][-1]["initial_status"] = clarify["status"]
            else:
                session = await new_session(browser)
                await browser.submit("assistant", "请先问我一个澄清问题，确认想了解的主题和目标；收到回答前不要使用任何工具。")
                with sqlite3.connect(run_root / "agent.db") as db:
                    run_id, session_id, status = db.execute(
                        "SELECT id,session_id,status FROM task_runs WHERE task_type='assistant' ORDER BY created_at DESC LIMIT 1"
                    ).fetchone()
                clarify = (await api.get(f"/api/tasks/{run_id}")).json()
                summary["runs"].append({"run_id": run_id, "initial_status": status})
            if clarify["status"] == "waiting":
                summary["checks"]["clarification_wait"] = True
                summary["runs"][-1]["pending_action"] = clarify.get("pending_action")
                assert clarify.get("pending_action", {}).get("action_type") == "clarification", clarify
                observe("clarification_submit_start", run_id=clarify["run_id"])
                await browser.wait("!!document.querySelector('.pending-action-panel textarea')", 25)
                await browser.input(".pending-action-panel textarea", "解释 Redis SETEX 的 TTL，优先使用本地知识证据。")
                await browser.click("提交补充说明")
                observe("clarification_submitted", run_id=clarify["run_id"])
            else:
                if clarify["status"] not in {"queued", "running"}:
                    raise RuntimeError(f"cannot resume existing Run from status={clarify['status']}")
                observe("resume_existing_execution", run_id=clarify["run_id"], status=clarify["status"])
            resumed = await wait_task(api, clarify["run_id"])
            summary["runs"][-1]["resumed_status"] = resumed["status"]
            assert resumed["status"] == "completed", resumed
            summary["checks"]["clarification_resume"] = True
            await browser.command("Page.reload")
            await browser.wait("document.querySelector('.assistant-result')?.innerText.length>0", 25)
            summary["checks"]["refresh_after_resume"] = True

            if args.single_run:
                summary["status"] = "completed_single_run"
                return

            existing_ids = {item["run_id"] for item in summary["runs"]}
            with sqlite3.connect(run_root / "agent.db") as db:
                current_runs = db.execute("SELECT id,session_id,status FROM task_runs WHERE task_type='assistant' ORDER BY created_at").fetchall()
            if not any(str(row[0]) not in existing_ids for row in current_runs):
                session = await new_session(browser)
                workspaces = (await api.get("/api/workspaces")).json()["workspaces"]
                fixture_workspace = next(item for item in workspaces if item["repo_path"].endswith("/project"))
                await bind_workspace(browser, fixture_workspace["workspace_id"])
                bound = await api.get("/api/sessions")
                bound.raise_for_status()
                selected = next(item for item in bound.json()["sessions"] if item["id"] == session)
                assert selected["default_workspace_id"] == fixture_workspace["workspace_id"]
                summary["fixture_workspace_id"] = fixture_workspace["workspace_id"]
                await browser.submit("assistant", "结合本地知识中的 Redis SETEX 资料与已注册项目文件 cache.py，说明该实现的 TTL，并给出来源。只读，不要执行测试。")
                with sqlite3.connect(run_root / "agent.db") as db:
                    cross_id, cross_workspace = db.execute(
                        "SELECT id,workspace_id FROM task_runs WHERE task_type='assistant' AND session_id=? "
                        "ORDER BY created_at DESC LIMIT 1", (session,)).fetchone()
                assert cross_workspace == fixture_workspace["workspace_id"]
                cross = await wait_task(api, cross_id)
                summary["runs"].append({"run_id": cross_id, "final_status": cross["status"]})
                with sqlite3.connect(run_root / "agent.db") as db:
                    tool_names = [row[0] for row in db.execute(
                        "SELECT tool_name FROM execution_operations WHERE run_id=? AND status='succeeded'", (cross_id,))]
                summary["runs"][-1]["successful_tools"] = tool_names
                summary["checks"]["cross_capability_terminal"] = cross["status"] == "completed"
                summary["checks"]["cross_capability_tools"] = "read_file" in tool_names and bool(
                    {"search_knowledge", "read_knowledge", "answer_knowledge"} & set(tool_names))
                await browser.command("Page.reload")
                await browser.wait("document.querySelectorAll('.task-card').length>0", 25)
                summary["checks"]["refresh_after_cross_capability"] = True

            if any(str(row[0]) not in existing_ids for row in current_runs):
                pending_cross = next((row for row in current_runs if str(row[0]) not in existing_ids), None)
                if pending_cross:
                    cross_id = str(pending_cross[0])
                    cross = await wait_task(api, cross_id)
                    with sqlite3.connect(run_root / "agent.db") as db:
                        tool_names = [row[0] for row in db.execute(
                            "SELECT tool_name FROM execution_operations WHERE run_id=? AND status='succeeded'", (cross_id,))]
                    if not any(item.get("run_id") == cross_id for item in summary["runs"]):
                        summary["runs"].append({"run_id": cross_id, "final_status": cross["status"],
                                                 "successful_tools": tool_names})
                    summary["checks"]["cross_capability_terminal"] = cross["status"] == "completed"
                    summary["checks"]["cross_capability_tools"] = "read_file" in tool_names and bool(
                        {"search_knowledge", "read_knowledge", "answer_knowledge"} & set(tool_names))

            if len(current_runs) < MAX_RUNS:
                await new_session(browser)
                await browser.submit("assistant", "请用两句话解释 Redis 持久化的主要权衡。")
                session = await browser.evaluate("localStorage.getItem('tomato-agent-session-id')")
                with sqlite3.connect(run_root / "agent.db") as db:
                    bounded_id = db.execute("SELECT id FROM task_runs WHERE task_type='assistant' ORDER BY created_at DESC LIMIT 1").fetchone()[0]
                bounded = await wait_task(api, bounded_id)
                summary["runs"].append({"run_id": bounded_id, "final_status": bounded["status"]})
                summary["checks"]["bounded_terminal_state"] = bounded["status"] in {"completed", "failed", "timed_out"}
                summary["checks"]["timeout_classified"] = bounded["status"] == "timed_out"
                await browser.command("Page.reload")
                await browser.wait("document.querySelectorAll('.task-card').length>0", 25)
                summary["checks"]["refresh_after_terminal"] = True
        summary["status"] = "passed" if all(summary["checks"].values()) else "partial_or_failed"
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