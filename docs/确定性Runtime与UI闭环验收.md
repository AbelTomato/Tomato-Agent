# 确定性 Runtime 与 UI 闭环验收

本入口使用明确标记为 `scripted` 的合成决策，不读取 Provider 录制、不调用真实模型、不产生 Provider 兼容性资格结论。它保留真实的决策解析、HTTP API、Worker、SQLite、只读工具和 React UI，不重构生产 Runtime，也不新增事件事实源或 SSE/WebSocket。

## 覆盖范围

- 固定协议序列：`message → ask_user → message → read_file → finish`。
- 阶段消息不完成 Run，澄清前不启动工具；补充说明恢复同一 Run。
- 旧版本及重复动作拒绝、取消后不继续消费脚本、脚本耗尽及非法决策明确失败。
- 持久化事件按序增量读取，工具启动/成功关联相同 invocation 和 Step。
- 浏览器验证等待状态刷新、提交补充说明、最终答案、完成后刷新和工作区未改动。
- 相同脚本期望相同业务状态和事件语义，不要求 UUID、时间戳或 Worker 调度事件逐字一致。最终固定文本用于检查 UI 数据传递，不代表评估模型答案质量。

## 单元及 API 集成测试

```bash
cd /home/abeltomato/workspace/projects/Tomato-Agent/backend
./.venv/bin/python -m pytest tests/test_scripted_runtime.py -q
```

测试使用独立测试目录，无真实 Provider。浏览器 fixture 的数据留在被忽略的 `backend/data/runtime-scripted-acceptance/`，不得提交 SQLite、截图或本地运行报告。该场景不涉及 RAG 语料、Embedding 或检索实验。

## 浏览器运行

要求现有依赖、可连接的 Chrome CDP，以及两个空闲端口。每次使用新的目录名；服务端拒绝已有 `agent.db`，不改写历史验收数据库。以下三个终端分别运行：

```bash
cd /home/abeltomato/workspace/projects/Tomato-Agent/backend
SCRIPTED_RUNTIME_ROOT=/home/abeltomato/workspace/projects/Tomato-Agent/backend/data/runtime-scripted-acceptance/my-new-run \
  ./.venv/bin/python -m uvicorn tests.fixtures.scripted_runtime_server:create_scripted_app \
  --factory --host 127.0.0.1 --port 8041
```

```bash
cd /home/abeltomato/workspace/projects/Tomato-Agent/frontend
VITE_API_BASE_URL=http://127.0.0.1:8041 pnpm dev --host 127.0.0.1 --port 5173 --strictPort
```

```bash
cd /home/abeltomato/workspace/projects/Tomato-Agent/backend
./.venv/bin/python -m tests.fixtures.scripted_runtime_browser \
  --root /home/abeltomato/workspace/projects/Tomato-Agent/backend/data/runtime-scripted-acceptance/my-new-run \
  --api-url http://127.0.0.1:8041 --frontend-url http://127.0.0.1:5173 \
  --cdp-url http://127.0.0.1:19222
```

默认前端端口与应用允许的 CORS origin 一致；调整端口时须相应配置允许来源。浏览器检查只创建和关闭自己的 CDP 标签，不关闭现有 Chrome。报告 `scripted-browser-summary.json` 明确记录 `mode=scripted`、`real_provider_qualification=false`；同时保存 `scripted-browser.png`。失败工件应保留，不能以自动重试、扩大预算或自动取消旧任务隐藏失败。

刷新检查先标记旧页面并等待新页面上下文，避免 `Page.reload` 返回后误读旧 DOM。一次失败可能留下等待任务及工作区占用，再跑应启动新隔离目录，而非覆盖旧库。

## 已验证边界

2026-10-18：脚本场景经真实 Chrome/CDP、Vite、HTTP、Worker、SQLite 执行通过，等待刷新、同 Run 恢复、完成刷新和文件未改动均通过。首次 harness 存在刷新竞态，修复后使用新隔离目录通过；复用失败运行目录曾被 `workspace_busy` 正确阻止。

该结果不证明真实模型会选择此轨迹，不覆盖进程重启、断线恢复、并发多 Run、写入副作用或实时事件 UI。真实 Provider 结果型 eval 和严格录制 replay 仍为独立入口。