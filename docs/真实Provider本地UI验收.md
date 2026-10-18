# 真实 Provider 本地 UI 验收

入口针对 `backend/.env` 当前配置的单个 LLM Provider/模型，以 Runtime v1 和本地助手 UI 为验收对象。它与 Fake Provider 测试分开；真实模型请求可能产生费用。真实兼容性结论必须限定到报告记录的 Provider、模型、端点主机、请求配置和验收日期，不外推到其他配置。

## 验证方向（2026-10-18 修订）

真实 LLM 验收以实际任务效果和业务不变量为准，不要求模型复现某次录制的工具发现顺序、请求次数、参数措辞或回答文本。允许不同合法轨迹；但协议/schema、权限、证据约束、用户确认和预算仍由程序强制，不因模型具有概率性而放宽。

| 验证层 | 目的与结论边界 |
| --- | --- |
| 真实 Provider + eval | 观察模型自主选择的合法轨迹，评估任务结果、澄清有效性、证据质量、安全性及成本；结论绑定模型、配置、场景和样本数。 |
| Fake/脚本式响应测试 | 注入明确标注的确定性决策，验证 Runtime 状态机、SQLite 持久化、恢复、工具协议和 UI；不证明真实模型会作出这些决策。 |
| 录制 replay | 消费已知响应/错误以定位历史故障、验证协议和确定性回归；录制是测试输入，不是 LLM 行为规范，也不要求新模型重走历史轨迹。 |

Observability 记录实际发生的请求、决策、工具 operation、事件、checkpoint 和 UI 观察，以关联 ID 串联并保留失败原因。Eval 基于这些事实检查结果和约束，不把某条固定轨迹作为唯一答案。多次运行应报告成功率、失败类别、调用次数、耗时及可获得的 token/费用数据；未采集指标标为缺失，单次成功不代表稳定质量通过。上述评测口径是方向约定，不表示当前 harness 已自动实现所有指标。

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

`--single-run` 不等于一次 Provider 请求。请求预算是费用和资源上限，不是预期步骤数，也不保证足够完成任务；实际调用不得超过用户批准的额度，不自动重试或扩额。预算内未完成应报告未完成及实际原因，不能据此要求模型压缩为固定轨迹，也不能直接断言为模型能力缺陷。

### 离线 replay

Replay 使用已有的 `provider-attempts.jsonl`，不创建或调用真实 Provider client；仍需 Chrome CDP 和本地 UI 环境。使用新的 `--run-id` 以隔离 replay 产物：

```bash
cd backend
../backend/.venv/bin/python tests/fixtures/runtime_provider_acceptance.py \
  --replay data/runtime-provider-acceptance/<source-run-id>/provider-attempts.jsonl \
  --run-id replay-<标识>
```

`--replay` 与 `--run-real-provider` 互斥，且不能与 `--resume` 或 `--stream` 组合。回放按录制身份和请求顺序严格匹配；缺少 `call_id`/请求内容、身份不匹配或录制耗尽会明确失败，不会猜测或自动重试。Replay 的 `summary.json` 标记 `mode: replay`，结果只用于离线解析、Runtime/UI 流程诊断，不构成真实 Provider 兼容性或资格通过结论。

以上严格匹配是当前实现的诊断约束，不是真实 LLM 业务验收标准。它有助于防止向错误请求投喂录制响应，不应为让任意轨迹通过而直接删除。确定性 Runtime/UI 测试采用独立的显式脚本式响应及阶段/协议断言，与真实运行区分。首期脚本入口及实际覆盖范围见[确定性 Runtime 与 UI 闭环验收](确定性Runtime与UI闭环验收.md)；它不改变严格 replay 行为，也不提供可切换的宽松匹配模式。

录制耗尽表示 fixture 不足以支持当前执行，不能合成 Provider 成功响应或宣称任务完成。失败录制也可用于失败路径回归，无须为凑齐固定步骤反复调用模型。保留来源、哈希、备份和替换记录；禁止混接不同请求的响应。替换 JSONL 不会更新原目录的 SQLite/截图/报告，旧工件不能作为新录制的配套运行证据。

### 恢复和判定

`--resume` 仅面向同一隔离目录的基础设施诊断恢复；它不会重置数据库或预算。已存在的 Run 不支持由场景执行器重新提交/修改，发现遗留活动场景会标为 `interrupted` 并要求新建验收批次。进程强制中断时，下次启动会恢复状态记录，但这不代表继续或重放了原 Run。执行结束后在该批次目录查看 `summary.json`、`scenarios.json`、`provider-attempts.jsonl`、`cdp-events.jsonl`、`server.log`、`frontend.log`、`agent.db` 及逐场景 UI JSON/PNG 诊断文件；以实际写入的文件为准。

## 判定

场景覆盖：澄清产生持久化等待、用户补充后同一 Run 继续并完成、Run 终态分类，以及刷新后恢复状态。跨能力场景必须观察到成功的 `read_file` 和知识工具 operation 才算通过。超时只有实际出现 `timed_out` 才记为通过；普通快速完成只证明有界终态和刷新恢复，不等同于超时恢复通过。真实 Provider 结论应逐项列出实际通过的场景，并绑定验收日期、Provider、模型、端点主机及当次有效请求配置。离线 replay 与 fake 测试不能作为真实兼容资格证据；没有真实验收记录时，不宣称真实 Provider 已通过。

### 澄清场景的结果与不变量

- 区分显式要求先提问的协议场景和自然缺信息场景；前者通过不能证明模型能自主判断何时需要澄清。
- 验证等待已持久化，补充前不执行场景禁止的操作；用户补充被消费后原 Run 恢复，不创建替代 Run。
- 不要求固定的 `discover_tools`、`describe_tool`、阶段消息顺序或模型调用次数。工具使用由任务需要决定；场景要求本地知识证据时，必须有实际成功的知识 operation 和可追溯证据，不能只凭“准备搜索”的文本通过。
- 成功场景必须完成任务、产出符合约束的回答并进入 `completed`；明确的失败终态只能证明失败处理，不能替代业务成功。预算、Provider、协议、工具和 UI 故障应分别归因。
- 对照 SQLite 事件、checkpoint、operation 和最终结果检查一致性；UI 等待、恢复、完成/失败及刷新恢复必须有真实 DOM/截图证据。后端状态字段不能冒充可见 UI 状态，空正文或停留“恢复中”视为未验证/异常。

### 2026-10-18 讨论与证据边界

`clarification-replay-20261018-01` 的四条请求全部匹配录制，SQLite 已观察到等待、`provide_input` 和同 Run 恢复；第四条响应仅描述 `search_knowledge`，未实际执行工具或完成回答。第五次模型调用在四次预算上限处被拦截，Run 为 `failed`，工具 operation 为 0；UI 截图停在“恢复中”。因此只能确认部分澄清链路，不能认定完整业务/UI 验收通过，也不能据此规定模型必须四步完成。该结论修正此前围绕“补齐固定第四条”的推进方向；后续优先完善可观测性及结果/不变量型 eval，真实请求仍须明确授权。
