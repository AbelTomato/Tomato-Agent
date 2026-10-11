"""Task-5 real Chrome checks; model chooses tools without scripted responses."""

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import httpx

from unified_browser_acceptance import Browser


async def controls(browser, summary):
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8020") as client:
        scenarios = [
            ("clarification", "I need help with an unspecified issue. Ask me what topic and desired outcome I mean before using any other capability.", "provide_input", {"message": "Briefly define TTL using general knowledge only. No retrieval or project reads."}),
            ("rejection", "Read cache.py in my project and explain its TTL. Request project binding if needed. If I refuse, explain that the project was not read and stop without accessing it.", "reject_action", {}),
        ]
        if os.environ.get("ASSISTANT_ACCEPTANCE_REJECTION_ONLY") == "1":
            scenarios = scenarios[1:]
        for scenario, prompt, action, payload in scenarios:
            await browser.click("新建会话")
            await browser.wait("document.querySelectorAll('.task-card').length===0", 20)
            await browser.submit("assistant", prompt)
            session = await browser.evaluate("localStorage.getItem('tomato-agent-session-id')")
            tasks = (await client.get(f"/api/sessions/{session}/tasks")).json()["tasks"]
            run = tasks[-1]
            summary["runs"].append(run)
            assert run["status"] == "waiting", run
            if action == "provide_input":
                assert run["pending_action"]["action_type"] == "clarification", run
                await browser.input(".pending-action-panel textarea", payload["message"])
                await browser.click("提交补充说明")
            else:
                assert run["pending_action"]["action_type"] == "workspace_binding", run
                await browser.click("拒绝此请求")
            await browser.wait("['已完成','失败','已取消','已超时'].includes([...document.querySelectorAll('.task-status')].at(-1)?.textContent)", 130)
            final = (await client.get(f"/api/tasks/{run['run_id']}")).json()
            summary["runs"][-1] = final
            assert final["status"] == "completed" and final["workspace_id"] is None, final
            summary["browser"][scenario] = True
            await browser.command("Page.reload")
            await browser.wait("document.querySelector('.assistant-result')?.innerText.length>0", 20)
            summary["browser"][scenario + "_refresh"] = True
    summary["status"] = "blocked_cross_capability_timeout_and_clarification_protocol"


async def main():
    root = Path(os.environ["ASSISTANT_ACCEPTANCE_ROOT"])
    project = root / "project"
    baseline = {item.name: hashlib.sha256(item.read_bytes()).hexdigest() for item in project.iterdir()}
    resume = os.environ.get("ASSISTANT_ACCEPTANCE_RESUME_CONTROLS") == "1"
    summary = json.loads((root / "browser-summary.json").read_text()) if resume else {"status": "running", "runs": [], "browser": {}}
    summary["status"] = "running"
    browser = Browser()
    await browser.connect()
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8020") as client:
            response = await client.post("/api/workspaces", json={"path": str(project), "pytest_paths": ["test_cache.py"]})
            response.raise_for_status()
            workspace = response.json()["workspace_id"]
        await browser.command("Page.navigate", url="http://127.0.0.1:5190")
        await browser.wait("!!document.querySelector('.composer textarea')", 20)
        await browser.evaluate("localStorage.removeItem('tomato-agent-session-id')")
        await browser.command("Page.reload")
        await browser.wait("!!document.querySelector('.composer textarea')", 20)
        await browser.click("新建会话")
        await browser.wait("!document.querySelector('.composer textarea').disabled", 20)
        session = await browser.evaluate("localStorage.getItem('tomato-agent-session-id')")
        summary["session_id"] = session
        assert await browser.evaluate("document.querySelector('.controls select').value") == "assistant"
        summary["browser"]["default_assistant"] = True

        async def snapshot():
            async with httpx.AsyncClient(base_url="http://127.0.0.1:8020") as client:
                result = await client.get(f"/api/sessions/{session}/tasks")
                result.raise_for_status()
                return result.json()["tasks"][-1]

        async def settled():
            await browser.wait("['已完成','失败','已取消','已超时','等待确认'].includes([...document.querySelectorAll('.task-status')].at(-1)?.textContent)", 130)
            return await snapshot()

        # Natural-language request names desired work, not a forced tool sequence.
        if resume:
            # Continue independent controls without retrying the failed real Run.
            await controls(browser, summary)
            return
        await browser.submit("assistant", "Using the local knowledge base and cache.py in my project, explain how SETEX makes cache_record expire and identify its TTL. Cite the retrieved source. Read only; do not run tests. If the project is not bound, request binding.")
        first = await snapshot()
        assert first["status"] == "waiting" and first["pending_action"]["action_type"] == "workspace_binding", first
        await browser.input(".pending-action-panel select", workspace)
        await browser.click("绑定并继续当前任务")
        await browser.wait("[...document.querySelectorAll('.task-status')].at(-1)?.textContent!=='等待确认'", 20)
        first = await settled()
        summary["runs"].append(first)
        assert first["status"] == "completed" and first["workspace_id"] == workspace, first
        assert first["result"]["citations"] and first["result"]["validation_status"] == "not_required", first
        assert "60" in first["result"]["answer"] and "SETEX" in first["result"]["answer"].upper(), first
        await browser.wait("!!document.querySelector('.assistant-result .citations')", 20)
        summary["browser"]["citations_and_binding"] = True
        await browser.click("查看已导入原文")
        await browser.wait("document.querySelector('.document-panel')?.innerText.includes('SETEX')", 20)
        summary["browser"]["source_document"] = True
        await browser.click("关闭")
        await browser.command("Page.reload")
        await browser.wait("document.querySelector('.assistant-result')?.innerText.includes('60')", 20)
        summary["browser"]["refresh_restored"] = True

        await browser.click("新建会话")
        await browser.wait("document.querySelectorAll('.task-card').length===0", 20)
        await browser.evaluate("[...document.querySelectorAll('nav button')].find(b=>b.textContent.includes(" + json.dumps(session) + ")).click()")
        await browser.wait("document.querySelector('.assistant-result')?.innerText.includes('60')", 20)
        summary["browser"]["session_switch_restored"] = True

        await browser.submit("assistant", "I need help with an unspecified issue. Please ask me what topic and desired outcome I mean before using any other capability.")
        second = await snapshot()
        assert second["status"] == "waiting" and second["pending_action"]["action_type"] == "clarification", second
        await browser.input(".pending-action-panel textarea", "Explain what TTL means briefly using general knowledge only; no retrieval or project reads needed.")
        await browser.click("提交补充说明")
        await browser.wait("[...document.querySelectorAll('.task-status')].at(-1)?.textContent!=='等待确认'", 20)
        second = await settled()
        summary["runs"].append(second)
        assert second["status"] == "completed" and second["result"]["answer"], second
        summary["browser"]["clarification_continued_same_run"] = True

        # A new Session has no workspace, so rejecting binding is observable.
        await browser.click("新建会话")
        await browser.wait("document.querySelectorAll('.task-card').length===0", 20)
        session = await browser.evaluate("localStorage.getItem('tomato-agent-session-id')")
        await browser.submit("assistant", "Read cache.py in my project and explain its TTL. Request project binding if needed; if I refuse, explain that the project has not been read and stop without accessing it.")
        third = await snapshot()
        assert third["status"] == "waiting" and third["pending_action"]["action_type"] == "workspace_binding", third
        await browser.click("拒绝此请求")
        await browser.wait("[...document.querySelectorAll('.task-status')].at(-1)?.textContent!=='等待确认'", 20)
        third = await settled()
        summary["runs"].append(third)
        assert third["status"] == "completed" and third["workspace_id"] is None, third
        summary["browser"]["rejection"] = True
        summary["status"] = "passed"
    except BaseException as exc:
        summary["status"] = "failed"
        summary["error_type"] = type(exc).__name__
        raise
    finally:
        try:
            summary["page_text"] = await browser.evaluate("document.body.innerText")
            image = await browser.command("Page.captureScreenshot", format="png")
            (root / "browser.png").write_bytes(base64.b64decode(image["data"]))
        finally:
            with sqlite3.connect(root / "agent.db") as db:
                db.row_factory = sqlite3.Row
                summary["operations"] = [dict(row) for row in db.execute("SELECT run_id,tool_name,status FROM execution_operations")]
                summary["ledger"] = [dict(row) for row in db.execute("SELECT run_id,scope,status FROM execution_model_calls")]
            summary["project_unchanged"] = baseline == {item.name: hashlib.sha256(item.read_bytes()).hexdigest() for item in project.iterdir()}
            (root / "browser-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            await browser.ws.close()
    assert summary["project_unchanged"]
    assert len(summary["ledger"]) <= 12
    tools = {op["tool_name"] for op in summary["operations"] if op["run_id"] == summary["runs"][0]["run_id"]}
    assert "read_file" in tools and tools & {"search_knowledge", "answer_knowledge"}
    assert not [op for op in summary["operations"] if op["run_id"] == summary["runs"][2]["run_id"]]
    print(json.dumps({"status": summary["status"], "run_ids": [run["run_id"] for run in summary["runs"]], "model_calls": len(summary["ledger"]), "browser": summary["browser"]}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())