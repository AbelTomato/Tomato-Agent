# 真实 Provider 本地 UI 验收

入口针对 `backend/.env` 当前配置的单个 LLM Provider/模型，以 Runtime v1 和本地助手 UI 为验收对象。它与 Fake Provider 测试分开；真实模型请求可能产生费用。

## 范围与隔离

- 只允许 `assistant` Run；最多 4 个 Run、最多 16 次模型请求，每 Run 600 秒。可通过 `--max-runs`、`--max-requests` 降低预算；预算耗尽不表示场景验收通过。
- 数据、日志和截图写入全新的 `backend/data/runtime-provider-acceptance/<run-id>/`。已有目录会拒绝复用。
- 使用独立 SQLite、固定只读项目 fixture 和本地知识 fixture；不调用 Embedding、Docker 或测试执行能力。
- API 中间件拒绝超出范围的 Run 和写/执行请求；不覆盖已有真实验收工件。
- 报告记录状态、Run ID、Provider 主机/模型标识、模型请求尝试序号和错误类别，不写 API Key。`provider-attempts.jsonl` 另保存每次返回的模型响应（kind/content/tool_call）用于协议诊断；其中可能包含用户输入相关内容，应按本地私有验收数据保管。

## 执行

前置条件：`backend/.env` 已配置目标 Provider；依赖已安装；本机 Chrome 以远程调试端口 `9222` 启动并打开任意页面。

常规入口在仓库根目录执行 `python3 scripts/verify.py external provider --confirm-real-dependencies --provider-max-runs 4 --provider-max-requests 16`，该包装入口最多运行 600 秒。Provider 明细仍保留在独立目录，包装入口的日志和结果位于 `verification-artifacts/`。需要指定 CDP 或恢复既有验收时使用下述底层诊断脚本。

```bash
cd backend
../backend/.venv/bin/python tests/fixtures/runtime_provider_acceptance.py --run-real-provider
```

可用 `--run-id <标识>` 指定隔离目录名，或用 `--cdp-url` 指定 Chrome CDP 地址。入口默认不运行；缺少 `--run-real-provider` 会直接退出。若验收因基础设施问题中断，可使用相同 `--run-id` 加 `--resume` 接续；历史 Provider 调用计入 16 次总上限，原隔离数据库不会重置。执行结束后报告位于该次 run 目录的 `summary.json`，浏览器截图为 `browser.png`，服务日志分为 `server.log` 和 `frontend.log`。

## 判定

场景覆盖：澄清产生持久化等待、用户补充后同一 Run 继续并完成、Run 终态分类，以及刷新后恢复状态。跨能力场景必须观察到成功的 `read_file` 和知识工具 operation 才算通过。超时只有实际出现 `timed_out` 才记为通过；普通快速完成只证明有界终态和刷新恢复，不等同于超时恢复通过。Provider 兼容结论只对本次 `.env` 中的模型配置有效，不外推到其他 Provider/模型。