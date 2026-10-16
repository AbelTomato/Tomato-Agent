# 真实 Provider 本地 UI 验收

入口针对 `backend/.env` 当前配置的单个 LLM Provider/模型，以 Runtime v1 和本地助手 UI 为验收对象。它与 Fake Provider 测试分开；真实模型请求可能产生费用。真实兼容性结论必须限定到报告记录的 Provider、模型、端点主机、请求配置和验收日期，不外推到其他配置。

## 范围与隔离

- 只允许 `assistant` Run；默认最多 4 个 Run、最多 16 次模型请求，每 Run 600 秒。通过 `--max-runs`、`--max-requests` 可降低上限（范围分别为 1–4、1–16）；场景预算进一步受全局剩余预算限制，不能扩大全局上限。预算耗尽不表示场景验收通过。
- 数据、日志和截图写入全新的 `backend/data/runtime-provider-acceptance/<run-id>/`。已有目录会拒绝复用。
- 使用独立 SQLite、固定只读项目 fixture 和本地知识 fixture；不调用 Embedding、Docker 或测试执行能力。
- API 中间件拒绝超出范围的 Run 和写/执行请求；不覆盖已有真实验收工件。
- `scenarios.json` 增量记录每个场景的状态、Run/请求关联、预算、预期与实际观察、失败阶段/类别及诊断文件。终态为 `passed`、`failed`、`timed_out`、`cancelled` 或恢复识别出的 `interrupted`；失败场景会停止后续场景。`summary.json` 汇总批次模式、状态、Run、场景和检查结果；Provider 请求、SQLite ledger/operation、CDP 事件、服务日志及 UI 截图/快照按索引关联。具体字段以工件为准。
- 真实报告应记录验收日期、Provider 标识、模型、端点主机、有效请求配置（包括 structured output/streaming）和实际通过的场景；不记录 API Key 或 Authorization Header。`provider-attempts.jsonl` 保存协议诊断所需的请求/响应内容，可能包含用户输入相关信息，必须作为本地敏感数据保管，不得提交。

## 执行

前置条件：`backend/.env` 已配置目标 Provider；依赖已安装；本机 Chrome 以远程调试端口 `9222` 启动并打开任意页面。

常规入口在仓库根目录执行 `python3 scripts/verify.py external provider --confirm-real-dependencies --provider-max-runs 4 --provider-max-requests 16`，该包装入口最多运行 600 秒。命令必须显式确认真实依赖，可能产生费用；它保持在常规 CI 之外。Provider 明细保留在独立目录，包装入口的日志和结果位于 `verification-artifacts/`。需要指定 CDP、streaming、单 Run 或诊断恢复时使用下述底层脚本。

```bash
cd backend
../backend/.venv/bin/python tests/fixtures/runtime_provider_acceptance.py --run-real-provider
```

可用 `--run-id <标识>` 指定隔离目录名，`--cdp-url <URL>` 指定 Chrome CDP 地址；`--stream` 开启 streaming，`--single-run` 限制为单 Run。预算参数 `--max-runs`、`--max-requests` 的默认值分别为 4、16。入口默认不运行；缺少 `--run-real-provider` 会直接退出。

### 离线 replay

Replay 使用已有的 `provider-attempts.jsonl`，不创建或调用真实 Provider client；仍需 Chrome CDP 和本地 UI 环境。使用新的 `--run-id` 以隔离 replay 产物：

```bash
cd backend
../backend/.venv/bin/python tests/fixtures/runtime_provider_acceptance.py \
  --replay data/runtime-provider-acceptance/<source-run-id>/provider-attempts.jsonl \
  --run-id replay-<标识>
```

`--replay` 与 `--run-real-provider` 互斥，且不能与 `--resume` 或 `--stream` 组合。回放按录制身份和请求顺序严格匹配；缺少 `call_id`/请求内容、身份不匹配或录制耗尽会明确失败，不会猜测或自动重试。Replay 的 `summary.json` 标记 `mode: replay`，结果只用于离线解析、Runtime/UI 流程诊断，不构成真实 Provider 兼容性或资格通过结论。

### 恢复和判定

`--resume` 仅面向同一隔离目录的基础设施诊断恢复；它不会重置数据库或预算。已存在的 Run 不支持由场景执行器重新提交/修改，发现遗留活动场景会标为 `interrupted` 并要求新建验收批次。进程强制中断时，下次启动会恢复状态记录，但这不代表继续或重放了原 Run。执行结束后在该批次目录查看 `summary.json`、`scenarios.json`、`provider-attempts.jsonl`、`cdp-events.jsonl`、`server.log`、`frontend.log`、`agent.db` 及逐场景 UI JSON/PNG 诊断文件；以实际写入的文件为准。

## 判定

场景覆盖：澄清产生持久化等待、用户补充后同一 Run 继续并完成、Run 终态分类，以及刷新后恢复状态。跨能力场景必须观察到成功的 `read_file` 和知识工具 operation 才算通过。超时只有实际出现 `timed_out` 才记为通过；普通快速完成只证明有界终态和刷新恢复，不等同于超时恢复通过。真实 Provider 结论应逐项列出实际通过的场景，并绑定验收日期、Provider、模型、端点主机及当次有效请求配置。离线 replay 与 fake 测试不能作为真实兼容资格证据；没有真实验收记录时，不宣称真实 Provider 已通过。