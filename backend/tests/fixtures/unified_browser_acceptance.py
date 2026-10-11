"""Drive installed Chrome via CDP against the isolated real acceptance server."""

import asyncio
import base64
import json
import os
from pathlib import Path

import httpx
import websockets


class Browser:
    async def connect(self):
        async with httpx.AsyncClient() as client:
            pages = (await client.get("http://127.0.0.1:9222/json/list")).json()
        page = next(item for item in pages if item["type"] == "page")
        self.ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=5_000_000)
        self.sequence = 0

    async def command(self, method, **params):
        self.sequence += 1
        await self.ws.send(json.dumps({"id": self.sequence, "method": method, "params": params}))
        while True:
            result = json.loads(await self.ws.recv())
            if result.get("id") == self.sequence:
                assert "error" not in result, result
                return result.get("result", {})

    async def evaluate(self, expression):
        result = await self.command("Runtime.evaluate", expression=expression,
                                    returnByValue=True, awaitPromise=True)
        assert "exceptionDetails" not in result, result
        return result["result"].get("value")

    async def wait(self, expression, timeout=330):
        async with asyncio.timeout(timeout):
            while not await self.evaluate(expression):
                await asyncio.sleep(0.25)

    async def click(self, text):
        await self.wait("[...document.querySelectorAll('button')].some(b=>b.textContent.includes(" + json.dumps(text) + ")&&!b.disabled)", 20)
        await self.evaluate("(() => {const b=[...document.querySelectorAll('button')].find(b=>b.textContent.includes(" +
            json.dumps(text) + "));if(!b||b.disabled)throw Error('button unavailable');b.click();})()")

    async def input(self, selector, value):
        await self.evaluate("(() => {const e=document.querySelector(" + json.dumps(selector) +
            ");Object.getOwnPropertyDescriptor(e instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:e instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype,'value').set.call(e," +
            json.dumps(value) + ");e.dispatchEvent(new Event(e instanceof HTMLSelectElement?'change':'input',{bubbles:true}));})()")

    async def latest(self):
        return await self.evaluate("document.querySelector('.task-card:last-of-type')?.innerText || [...document.querySelectorAll('.task-card')].at(-1)?.innerText")

    async def submit(self, kind, message):
        await self.input(".controls select", kind)
        await self.wait("!document.querySelector('.composer textarea').disabled", 20)
        before = await self.evaluate("document.querySelectorAll('.task-card').length")
        await self.input(".composer textarea", message)
        await self.click("提交任务")
        await self.wait(f"document.querySelectorAll('.task-card').length>{before}", 20)
        await self.wait("['已完成','失败','已取消','已超时','等待确认'].includes([...document.querySelectorAll('.task-status')].at(-1)?.textContent)")
        return await self.evaluate("[...document.querySelectorAll('.task-card')].at(-1).innerText")

    async def diff(self):
        await self.evaluate("[...document.querySelectorAll('.task-card')].at(-1).querySelector('.code-artifacts').closest('details').open=true")
        await self.evaluate("[...document.querySelectorAll('.task-card')].at(-1).querySelectorAll('.code-artifacts button').forEach(b=>{if(b.textContent.includes('查看本轮 diff'))b.click()})")
        await self.wait("[...document.querySelectorAll('.task-card')].at(-1).innerText.includes('--- a/calculator.py')", 20)
        return await self.evaluate("[...document.querySelectorAll('.task-card')].at(-1).querySelector('.code-artifacts').parentElement.querySelector('pre').textContent")


async def main():
    root = Path(os.environ["UNIFIED_ACCEPTANCE_ROOT"])
    assert root.is_absolute() and root.is_relative_to(Path(__file__).parents[2] / "data" / "unified-mvp-acceptance")
    browser = Browser()
    await browser.connect()
    resume = os.environ.get("UNIFIED_BROWSER_RESUME") == "1"
    summary = json.loads((root / "browser-summary.json").read_text()) if resume else {}
    try:
        await browser.command("Page.navigate", url="http://127.0.0.1:5188")
        await browser.wait("!!document.querySelector('.conversation-shell')", 20)
        if os.environ.get("UNIFIED_BROWSER_CONTROLS_RESUME") == "1":
            await browser.wait("!document.querySelector('.composer textarea').disabled", 20)
            await browser.click("新建会话")
            await browser.wait("document.querySelectorAll('.task-card').length===0", 20)
            summary["second_session_id"] = await browser.evaluate("localStorage.getItem('tomato-agent-session-id')")
            await browser.evaluate("[...document.querySelectorAll('nav button')].find(b=>b.textContent.includes(" + json.dumps(summary["session_id"]) + ")).click()")
            await browser.wait("document.querySelectorAll('.task-card').length===6", 20)
            summary["session_switch_restored"] = True
            await controls(browser, summary, root)
            image = await browser.command("Page.captureScreenshot", format="png")
            (root / "browser.png").write_bytes(base64.b64decode(image["data"]))
            return
        if not resume:
            await browser.click("新建会话")
            await browser.wait("!document.querySelector('.composer textarea').disabled", 20)
            summary["session_id"] = await browser.evaluate("localStorage.getItem('tomato-agent-session-id')")
            summary["chat"] = await browser.submit("chat", "Reply only: unified acceptance succeeded. Use English.")
            assert "已完成" in summary["chat"], summary["chat"]
            summary["knowledge"] = await browser.submit("knowledge", "How does Redis SETEX set key expiration? Answer in English.")
        else:
            await browser.wait("document.querySelectorAll('.task-card').length>=2", 20)
        assert "已完成" in summary["knowledge"] and "查看已导入原文" in summary["knowledge"], summary["knowledge"]
        await browser.click("查看已导入原文")
        await browser.wait("!!document.querySelector('.document-panel .document-chunk')", 20)
        summary["source_opened"] = True
        if os.environ.get("UNIFIED_BROWSER_CODE_RESUME") == "1":
            if os.environ.get("UNIFIED_BROWSER_RETRY_MULTIPLY") != "1":
                summary["code_add"] = await browser.evaluate("[...document.querySelectorAll('.task-card')].at(-1).innerText")
                summary["diff_add"] = await browser.diff()
        else:
            await writing(browser, summary)
        await browser.evaluate("document.querySelector('.workspace-picker').open=true")
        if not await browser.evaluate("!!document.querySelector('.workspace-picker select').value"):
            await browser.input(".workspace-picker input", str(root / "browser-project"))
            await browser.input(".workspace-picker label:nth-of-type(3) input", "test_calculator.py")
            await browser.click("注册并绑定目录")
            await browser.wait("!!document.querySelector('.workspace-picker select').value", 20)
        for function, operator in (("multiply", "*"),) if os.environ.get("UNIFIED_BROWSER_CODE_RESUME") == "1" else (("add", "+"), ("multiply", "*")):
            message = f'Read calculator.py and test_calculator.py. Fix only {function} to return a {operator} b, preserve other functions and files. Do not edit tests. Call run_tests with exactly {{"target":"unit"}} (no arguments), and get_diff then finish.'
            if os.environ.get("UNIFIED_BROWSER_RETRY_MULTIPLY") == "1":
                message = 'Read calculator.py and test_calculator.py. Add the docstring "Multiply two numbers." inside multiply, preserve its correct a * b implementation and all other files. Call run_tests with exactly {"target":"unit"}, no arguments or other target, then get_diff and finish.'
            result = await browser.submit("code", message)
            summary[f"code_{function}"] = result
            assert "已完成" in result and "测试 1：通过" in result, result
            summary[f"diff_{function}"] = await browser.diff()
        await browser.command("Page.reload")
        count = 6 if os.environ.get("UNIFIED_BROWSER_RETRY_MULTIPLY") == "1" else 5
        await browser.wait(f"document.querySelectorAll('.task-card').length==={count}", 20)
        summary["refresh_restored"] = True
        await browser.click("新建会话")
        await browser.wait("document.querySelectorAll('.task-card').length===0", 20)
        summary["second_session_id"] = await browser.evaluate("localStorage.getItem('tomato-agent-session-id')")
        await browser.evaluate("[...document.querySelectorAll('nav button')].find(b=>b.textContent.includes(" + json.dumps(summary["session_id"]) + ")).click()")
        await browser.wait(f"document.querySelectorAll('.task-card').length==={count}", 20)
        summary["session_switch_restored"] = True
        await controls(browser, summary, root)
        image = await browser.command("Page.captureScreenshot", format="png")
        (root / "browser.png").write_bytes(base64.b64decode(image["data"]))
    finally:
        (root / "browser-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        await browser.ws.close()


async def writing(browser, summary):
    summary["writing_outline"] = await browser.submit("writing", "SETEX expiration")
    assert "confirm_outline" in summary["writing_outline"], summary["writing_outline"]
    outline = await browser.evaluate("JSON.parse(document.querySelector('.task-card textarea').value)")
    outline["title"] = "用户编辑 SETEX 过期机制"
    await browser.input(".task-card textarea", json.dumps(outline, ensure_ascii=False))
    await browser.click("确认提纲并生成草稿")
    await browser.wait("[...document.querySelectorAll('button')].some(b=>b.textContent==='确认保存草稿')")
    await browser.click("确认保存草稿")
    await browser.wait("[...document.querySelectorAll('.task-card')].at(-1)?.innerText.includes('草稿已保存')")
    summary["writing_saved"] = await browser.evaluate("[...document.querySelectorAll('.task-card')].at(-1).innerText")


async def controls(browser, summary, root):
    # Cancel a real Provider request while its unified Run is running.
    await browser.input(".controls select", "chat")
    await browser.input(".composer textarea", "请详细解释 Redis 持久化的设计权衡。")
    await browser.click("提交任务")
    await browser.wait("[...document.querySelectorAll('.task-status')].at(-1)?.textContent==='执行中'", 30)
    await browser.click("取消任务")
    await browser.wait("[...document.querySelectorAll('.task-status')].at(-1)?.textContent==='已取消'", 30)
    summary["active_cancel"] = True
    # An external edit after the model has observed the file must remain intact.
    await browser.input(".controls select", "code")
    await browser.input(".composer textarea", "Read calculator.py, then change add to return a + b + 0, run unit and get_diff. Do not edit other files.")
    await browser.click("提交任务")
    import sqlite3
    async with asyncio.timeout(120):
        while True:
            with sqlite3.connect(root / "browser-agent.db") as db:
                ident = db.execute("SELECT id FROM task_runs ORDER BY rowid DESC LIMIT 1").fetchone()[0]
                seen = db.execute("SELECT 1 FROM execution_operations WHERE run_id=? AND tool_name='read_file' AND status='succeeded'", (ident,)).fetchone()
            if seen:
                break
            await asyncio.sleep(0.1)
    sentinel = "# external editor owns this content\ndef add(a, b):\n    return a + b\n\ndef multiply(a, b):\n    return a * b\n"
    (root / "browser-project" / "calculator.py").write_text(sentinel)
    await browser.wait("['失败','已取消','已超时'].includes([...document.querySelectorAll('.task-status')].at(-1)?.textContent)")
    assert (root / "browser-project" / "calculator.py").read_text() == sentinel
    summary["external_conflict_rejected"] = True
    summary["conflict_run_id"] = ident


if __name__ == "__main__":
    asyncio.run(main())
