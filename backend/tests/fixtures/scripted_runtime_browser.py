"""Offline browser checks against an already running scripted fixture server."""

import argparse
import asyncio
import base64
import json
from pathlib import Path

import httpx

from tests.fixtures.scripted_runtime import FINAL_ANSWER, STAGE_MESSAGE
from tests.fixtures.unified_browser_acceptance import Browser


async def reload_page(browser):
    await browser.evaluate("window.__scriptedReloadPending = true")
    await browser.command("Page.reload")
    await browser.wait("window.__scriptedReloadPending !== true && !!document.querySelector('.conversation-shell')", 20)


async def check(args):
    root = args.root.resolve()
    allowed = Path(__file__).resolve().parents[2] / "data" / "runtime-scripted-acceptance"
    if not root.is_relative_to(allowed) or root == allowed or not (root / "agent.db").is_file():
        raise ValueError("select the running scripted server's isolated directory")
    summary = {"mode": "scripted", "real_provider_qualification": False, "status": "running"}
    browser = Browser()
    target_id = None
    async with httpx.AsyncClient(base_url=args.api_url, timeout=10) as api, httpx.AsyncClient() as cdp:
        try:
            target = await cdp.put(args.cdp_url + "/json/new?" + args.frontend_url)
            target.raise_for_status()
            target_id = target.json()["id"]
            await browser.connect(args.cdp_url, target_id=target_id)
            await browser.wait("!!document.querySelector('.conversation-shell')", 20)
            await browser.click("新建会话")
            await browser.wait("!!document.querySelector('.composer textarea') && !document.querySelector('.composer textarea').disabled", 20)
            session = await browser.evaluate("localStorage.getItem('tomato-agent-session-id')")
            response = await api.post("/api/workspaces", json={"path": str(root / "project"), "pytest_paths": ["tests"]})
            response.raise_for_status()
            workspace = response.json()["workspace_id"]
            response = await api.post(f"/api/sessions/{session}/workspace", json={"workspace_id": workspace})
            response.raise_for_status()
            await reload_page(browser)
            await browser.wait("!!document.querySelector('.composer textarea') && !document.querySelector('.composer textarea').disabled", 20)
            await browser.input(".composer textarea", "Run the scripted read-only clarification scenario.")
            await browser.click("提交任务")
            await browser.wait("document.querySelector('.task-status')?.textContent==='等待确认'", 20)
            tasks = (await api.get(f"/api/sessions/{session}/tasks")).json()["tasks"]
            assert len(tasks) == 1
            run_id = tasks[0]["run_id"]
            summary["run_id"] = run_id
            await reload_page(browser)
            await browser.wait("!!document.querySelector('.pending-action-panel textarea') && !document.querySelector('.pending-action-panel textarea').disabled", 20)
            assert await browser.evaluate("document.querySelectorAll('.task-card').length") == 1
            summary["waiting_refresh"] = True
            assert STAGE_MESSAGE in await browser.evaluate("document.querySelector('.task-events').textContent")
            await browser.input(".pending-action-panel textarea", "cache.py")
            await browser.click("提交补充说明")
            await browser.wait("document.querySelector('.task-status')?.textContent==='已完成'", 20)
            assert FINAL_ANSWER in await browser.evaluate("document.querySelector('.assistant-result').innerText")
            final = (await api.get(f"/api/tasks/{run_id}")).json()
            assert final["status"] == "completed" and final["pending_action"] is None
            events = (await api.get(f"/api/tasks/{run_id}/events")).json()["events"]
            for event_type in ("tool.started", "tool.succeeded"):
                assert sum(e["event_type"] == event_type for e in events) == 1
            await reload_page(browser)
            await browser.wait("document.querySelector('.task-status')?.textContent==='已完成' && !document.querySelector('.composer textarea').disabled", 20)
            assert FINAL_ANSWER in await browser.evaluate("document.querySelector('.assistant-result').innerText")
            tasks = (await api.get(f"/api/sessions/{session}/tasks")).json()["tasks"]
            assert [t["run_id"] for t in tasks] == [run_id]
            assert (root / "project" / "cache.py").read_text() == "CACHE_TTL = 60\n"
            summary.update(status="passed", same_run_resumed=True, completed_refresh=True,
                           project_unchanged=True, event_types=[e["event_type"] for e in events])
        except BaseException as exc:
            summary.update(status="failed", error_type=type(exc).__name__, error=str(exc)[:500])
            raise
        finally:
            if getattr(browser, "ws", None):
                try:
                    summary["page_text"] = await browser.evaluate("document.body.innerText")
                    image = await browser.command("Page.captureScreenshot", format="png")
                    (root / "scripted-browser.png").write_bytes(base64.b64decode(image["data"]))
                except Exception as exc:
                    summary["diagnostic_error"] = type(exc).__name__
                await browser.ws.close()
            (root / "scripted-browser-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            if target_id:
                await cdp.put(args.cdp_url + "/json/close/" + target_id)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8041")
    parser.add_argument("--frontend-url", default="http://127.0.0.1:5173")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:19222")
    asyncio.run(check(parser.parse_args()))