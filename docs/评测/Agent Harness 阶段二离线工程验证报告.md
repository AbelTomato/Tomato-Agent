# Agent Harness 阶段二离线工程验证报告

> **报告性质：** Fake/离线工程验证，不是真实模型质量验收、容量测试或 OpenShell 安全认证。
> **工程核对日期：** 2026-10-04；当前状态同步：2026-10-09。历史工程检查不重跑，后续真实对照与 Sandbox 证据分别列明。

## 1. 结论摘要

阶段二的 Harness 契约、预算和工具边界、受限自主研究、Sandbox 失败关闭契约及离线评测管线具备自动化测试证据。本报告记录的 Fake 对照用于验证固定任务 schema、哈希、机器指标字段、provenance 和报告落盘契约；其中的完成率、延迟、调用数来自合成 Fake 数据，不代表真实 Provider 或实际业务路径的性能、质量或成本。

**阶段二工程验证：通过本报告列出的自动检查。**

**当前结论：阶段二固定样本工程与人工质量验收通过，完整准入门禁仍未完成。** 第 5 节已记录 5 题真实对照、10/10 案例完成、人工评分 9.2/10 与 9.6/10，以及无答案拒答 2/2；3 个案例存在最终引用缺失。小样本不能证明稳定策略收益；研究与写作生产路径公共 Harness 接入证据及更大范围质量评审仍不足，阶段三门禁保持关闭。

## 2. 范围与实现边界

- 共享执行边界由 `app.agent.harness`、`harness_models` 和 `policies` 提供；Harness 管理模型决策循环、上下文及预算、工具 allowlist、参数校验、停止条件和结果边界，不接管 WritingTask 状态迁移。
- `AutonomousResearcher.collect()` 默认使用 deterministic 模式；`constrained_autonomous` 必须显式选择，工具限制在批准的只读知识检索/阅读范围内。
- 写作业务状态、任务版本、人工提纲确认和保存确认仍由现有服务端状态机控制。
- Sandbox 测试验证请求/结果契约、失败关闭和输出限制。默认无隔离 backend 时拒绝执行；注入的本地测试 backend 只是 Fake，不证明操作系统级隔离。
- 本报告原始离线工程检查未连接 OpenShell 服务。后续 2026-10-09 已独立完成 Docker/OpenShell CLI Adapter 基础真实验收与代码任务受限 MVP；见受控代码任务说明和当日日志，不计入本报告的离线测试结果。

模型只可在服务端批准范围内选择只读工具、继续或停止。程序强制工具白名单、schema、循环/工具/时长/上下文/响应预算、失败停止、业务状态和人工确认。

## 3. 离线评测配置与产物

离线入口：`backend/evals/harness_stage2.py`。

报告产物：

`/home/abeltomato/workspace/projects/Tomato-Agent/backend/data/rag/reports/2026-10-03/harness-stage-2/`

- `summary.json`
- `cases.jsonl`
- `provenance.json`

当前正式固定任务集位于 `backend/evals/harness_stage2_taskset.json`，包括研究 2 题、写作 3 题，其中 1 题为无答案拒答样本。加载器从正式 `dev` 题集和只读 RAG 快照生成完整证据快照，校验两项源文件 SHA-256、文档版本、行号范围和引用文本；每题分别生成 deterministic 与 constrained_autonomous 两条记录，总计 10 条。报告记录 task set / snapshot SHA-256、模型标识、prompt/runtime 版本和预算配置。Provenance 标明离线 Fake 运行时 `provider=fake`、`external_requests=false`；token usage 为 `unknown`。

产物中的机器指标（仅描述这次合成 Fake 输出）：完成率 1.0、结构校验率 1.0、调用数 2、模型调用数 6、合成 p50 延迟 15ms、合成 p95 延迟 20ms。**这些指标不得外推为真实系统效果或容量。**

## 4. 本轮工程验证

使用项目后端虚拟环境 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/.venv` 执行：

| 验证范围 | 结果 |
| --- | ---: |
| Harness 模型、编排、策略测试 | 18 passed |
| Sandbox、自主研究、Harness 集成测试 | 13 passed |
| Context、Runtime、Tools、写作研究及写作执行器回归 | 54 passed |
| Harness 离线评测 schema/产物测试 | 4 passed |
| 合计 | **89 passed** |

` .venv/bin/python -m compileall -q app evals tests` 通过；`git diff --check` 通过。真实 Provider、OpenShell 和人工语义验收均未执行。

## 5. 真实 Provider 阶段二收尾对照

- 执行入口：`backend/evals/harness_stage2.py --real-provider --max-calls 10`。
- 固定范围：5 个任务、deterministic 与 constrained_autonomous 两种策略，共 10 次调用；题集和 RAG 快照哈希与固定 manifest 一致。
- 报告目录：`backend/data/rag/reports/2026-10-03/harness-stage-2-real/`。
- 结果：10/10 Provider 调用成功，10/10 案例完成，调用索引连续为 1–10；`external_requests=true`。
- `task_set_sha256`：`36d38bed1ebdee8642f912d011beddea0ded1fd832ac89eabf6a3c2eb2d2725d`。
- `snapshot_sha256`：`dc9fa280fd473a608de22ca3585ebd399c389817e7fe61555e121133c629fcc8`。
- 已检查响应和报告产物，未发现 API key、Authorization Header 或密钥前缀；token usage 仅记录 Provider 返回的计数。
- 人工评分已写入 `backend/data/rag/reports/2026-10-03/harness-stage-2-real/human-review.json`：deterministic **46/50（9.2/10）**，constrained_autonomous **48/50（9.6/10）**；无答案拒答正确率 **2/2**。两种策略均通过本固定样本的基本语义验收；3 个案例存在内容正确但最终引用缺失的问题。
- 本轮可判定为“阶段二固定样本验收通过”，但样本量仅 5 题，不能据此宣称稳定质量收益、产品级质量或容量结论，也不能单独作为阶段三准入依据；生产路径公共 Harness 接入证据仍需独立验证。

## 6. 未完成门禁与后续要求

| 门禁 | 当前状态 | 解除条件 |
| --- | --- | --- |
| 真实固定任务 deterministic/autonomous 质量对照 | 已完成 | 扩大样本或变更 Provider/提示版本时重新执行对照 |
| 证据相关性、事实覆盖、引用归属、无答案拒答判断 | 固定样本已完成 | 补充更大样本并继续保持机器指标与人工结论分开记录 |
| 研究与写作生产路径使用公共 Harness 的证据 | 不充分 | 核查/补齐真实应用接入证据；不得以 Fake 双任务测试替代 |
| Sandbox 契约失败关闭 | 自动测试通过 | 只证明本地契约行为，不等于隔离能力认证 |
| OpenShell 真实集成 | 后续独立基础验收与代码任务受限 MVP 已通过 | 见受控代码任务说明；生产前仍需完整资源/生命周期、失联及取消故障矩阵，不以基础验收替代 |
| 阶段三门禁 | 关闭 | 仍需生产路径接入证据及独立的更大范围质量评审 |

本报告记录的真实 Provider 调用已获得本轮明确批准，仅限固定 5 题、最多 10 次调用。后续其他外部模型请求、部署服务或 OpenShell 操作仍需单独批准。

## 7. 数据安全与解释限制

- 本轮报告与自动测试没有要求写入业务数据库；实验产物使用规定的 RAG reports 目录。
- Fake 成功率、延迟和调用数是测试夹具产生的数据，不是抽样测量值。
- 自动 schema/结构校验通过不代表语义正确、事实受证据支持或引用归属通过。
- Sandbox Fake backend 测试不能作为宿主机隔离、网络隔离或凭据保护的实证。
- 当前结论为“阶段二固定样本工程与人工质量验收通过”；该结论不等于产品级质量保证，也不自动打开阶段三门禁。