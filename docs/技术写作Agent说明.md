# 技术写作 Agent 说明

## Harness 阶段二：共享边界与当前限制

阶段二新增了可供受控任务复用的 `app.agent.harness` 编排契约，以及 `Budget`、`ToolPolicy`、`HarnessState`、`ExecutionResult` 和审计模型。Harness 管理模型决策循环、上下文预算、工具白名单/参数校验、工具调用与时长限制、输出限制和执行结果；它不迁移或直接修改写作任务状态。现有确定性研究仍由 `WritingResearcher.collect()` 单次检索，`AutonomousResearcher.collect()` 默认 `mode="deterministic"`，只有显式指定 `constrained_autonomous` 才进入有界研究循环；未配置模型时自主模式失败关闭。自主研究工具只允许知识检索/阅读，不提供写入、保存或 Sandbox Policy 工具。

模型可在服务端批准的只读工具范围内选择检索、阅读或停止；程序强制工具白名单、参数 schema、循环/工具/时长/上下文/响应预算、错误停止，以及写作业务状态、版本冲突和用户提纲/保存确认。证据和主题作为不可信动态内容处理，研究结果仍须经过既有证据和写作校验。`CapabilityProfile` 默认拒绝网络、进程和凭据能力；本地 Sandbox 无隔离后端时拒绝执行，OpenShell 适配器当前也只返回不可用。注入的本地测试 backend 仅用于确定性契约测试，不代表真实隔离或 OpenShell 集成。

阶段二评测入口为 `backend/evals/harness_stage2.py`，正式固定任务集定义在 `backend/evals/harness_stage2_taskset.json`，由 `harness_stage2_taskset.py` 从冻结 `dev` 题集和只读 RAG 快照加载 5 题（研究 2 题、写作 3 题），包含 1 个无答案拒答样本；真实 Provider 对照使用 deterministic 与 constrained_autonomous 两组、最多 10 次调用。加载器校验题集/快照 SHA-256、文档版本、证据行范围和完整引用文本，并记录任务/快照 provenance。阶段二固定样本人工验收结果为 deterministic 9.2/10、constrained_autonomous 9.6/10，无答案拒答 2/2；结果记录在 `backend/data/rag/reports/2026-10-03/harness-stage-2-real/human-review.json`。两种策略均通过固定样本基本语义验收，但样本量不足以证明稳定质量收益，且阶段三仍需生产路径接入证据和更大范围质量评审。

## 当前已实现范围

技术写作任务由服务端状态机控制，当前支持：

1. 创建任务：`researching`。
2. 服务端发布提纲和引用快照：`awaiting_outline_confirmation`。
3. 用户携带版本确认提纲：`drafting`。
4. 服务端提交草稿：`awaiting_save_confirmation`。
5. 用户携带版本和幂等键保存：`saved`。

模型输出不能直接表示用户已经确认。确认和保存都必须由对应 HTTP API 接收用户请求，并校验任务状态与版本。

首期研究执行器已经覆盖“检索证据 → 生成提纲 → 校验引用 → 原子发布”的切片，但边界停在人工确认前。创建任务不会自动执行，也没有持久化队列或后台 Worker。

前端使用与服务端状态一致的显式阶段交互：创建任务后由用户点击“开始研究”；研究完成后展示提纲、引用来源和证据状态，用户编辑并确认提纲；确认后由用户再次点击“生成草稿”；草稿生成成功后以只读方式展示正文和引用归属，用户点击“确认保存草稿”。页面刷新只查询任务和最近草稿 attempt，不自动重放研究或草稿生成。只读生成阶段失败使用生成重试，保存阶段失败继续复用同一幂等键重试，不通过生成接口恢复保存。

## API

### 创建任务

```http
POST /api/sessions/{session_id}/writing-tasks
Content-Type: application/json

{"topic":"Redis 缓存实践"}
```

### 查询任务

```http
GET /api/writing-tasks/{task_id}
```

### 显式研究并生成提纲

```http
POST /api/writing-tasks/{task_id}/research
Content-Type: application/json

{"version":1}
```

请求只接受严格正整数 `version`，不接受客户端提纲、路径、模型或预算字段。执行器固定当前任务版本，使用写作专用 `KnowledgeService` 检索一次证据，调用一次注入的提纲模型，并把完整证据快照与提纲放在同一提交边界内。成功响应状态为 `awaiting_outline_confirmation`，不会生成草稿或文件。

最近一次执行尝试：

```http
GET /api/writing-tasks/{task_id}/research-attempt
```

该接口只返回 attempt 的阶段、状态、输入版本、Run ID、配置限制、模型标识和稳定错误码。研究 attempt 的 `config` 还记录请求的 `retrieval_mode`、实际 `actual_retrieval_mode` 以及可选的 `retrieval_fallback_reason`。重复提交已成功的相同输入版本返回当前任务，不再次调用模型；`running`、版本冲突或过期结果返回 `409`。进程硬退出留下的 `running` 记录不会自动接管。

## 研究限制与错误

默认 `retrieval_mode=hybrid`，先尝试关键词与向量融合；应用装配和写作研究会注入已配置的 Embedding 客户端。若 hybrid 因 Embedding 客户端/配置缺失或向量索引模型、维度不兼容而不可行，服务端只对这些已知情况回退到 `keyword`，并在回答、研究快照和 attempt 配置中记录实际模式与回退原因；Embedding Provider 网络、认证、超时等真实错误不会被吞掉。中文主题仍可能召回主题无关材料，非空证据也不等于语义质量通过；空证据不会调用提纲模型。

### 写作路由与状态

```text
POST /api/sessions/{session_id}/writing-tasks
  -> researching
POST /api/writing-tasks/{task_id}/research
  -> awaiting_outline_confirmation
POST /api/writing-tasks/{task_id}/confirm-outline
  -> drafting
POST /api/writing-tasks/{task_id}/draft
  -> awaiting_save_confirmation
POST /api/writing-tasks/{task_id}/save
  -> saved
```

`GET /api/writing-tasks/{task_id}` 查询任务；`GET /research-attempt` 和 `GET /draft-attempt` 查询对应阶段的安全执行记录；`POST /retry` 只用于可重试的生成失败。每次迁移都由服务端校验状态和版本，前端不得跳过确认阶段。

常见错误映射如下：

| HTTP | `detail.code` |
| --- | --- |
| 422 | `evidence_insufficient`、`context_budget_exceeded`、`outline_invalid`、`draft_invalid`、`citation_invalid` |
| 502 | `retrieval_failed`、`provider_failed`、`invalid_model_response`、`invalid_citation` |
| 503 | `model_unconfigured`、`publication_failed`、`storage_unavailable` |
| 504 | `deadline_exceeded` |

模型只能返回结构化提纲和服务端证据 ID；服务端不信任模型提供的路径、行号或正文。通过校验不代表语义支撑已经人工验收。研究 Run 是独立阶段记录，不写入普通聊天消息，也不复用其他 Session 的历史上下文。

### 确认提纲

```http
POST /api/writing-tasks/{task_id}/confirm-outline
Content-Type: application/json

{"version":2,"outline":{"title":"Redis 缓存实践","sections":[{"title":"过期策略","points":["设置过期时间"],"citation_ids":["chunk-1"]}],"gaps":[]}}
```

确认请求只接受 `version` 和 `outline`，请求体中的额外控制字段（例如 `saved_path`、`status`）会被拒绝。`outline` 必须包含 `title`、非空 `sections` 和 `gaps`；每个 section 必须包含 `title`、`points` 和非空 `citation_ids`。用户可以编辑标题、章节和要点，但 citation ID 必须来自研究阶段保存的证据快照，不能重复或引用未知证据。

结构或引用校验失败返回稳定的 `detail.code`（`invalid_model_response` 或 `invalid_citation`），不返回正文、路径或 traceback，且任务仍保持 `awaiting_outline_confirmation`。版本过期、状态不匹配或并发更新返回 `409`，不会覆盖当前任务。

### 生成草稿

```http
POST /api/writing-tasks/{task_id}/draft
Content-Type: application/json

{"version":3}
```

请求只接受严格正整数 `version`，拒绝客户端提供的草稿正文、路径、模型、提示词或预算字段。任务必须已经确认提纲并处于 `drafting`；成功响应状态为 `awaiting_save_confirmation`，不会写入保存文件。

最近一次草稿执行尝试：

```http
GET /api/writing-tasks/{task_id}/draft-attempt
```

该接口只返回草稿 attempt 的安全字段；任务没有草稿执行记录时返回 `200 + null`，任务不存在返回 `404`。重复成功请求返回当前任务，不重复调用模型；运行中、版本过期或状态不匹配返回 `409`。

### 保存草稿

```http
POST /api/writing-tasks/{task_id}/save
Content-Type: application/json

{"version":4,"idempotency_key":"save-2026-09-21-001"}
```

请求不接受目标文件路径。相同任务和幂等键的重复请求返回同一保存结果；不同幂等键不能为已保存任务生成第二份产物。

## 保存目录与恢复边界

保存目录由 `draft_directory` 配置，默认是 `backend/data/drafts`（相对于后端工作目录）。目标文件名由任务 ID 和草稿版本生成。服务端先在同目录写入临时文件并执行原子替换，再以条件状态/版本更新数据库。

数据库和文件系统之间不宣称分布式事务或恰好一次执行。如果文件已经生成但数据库状态更新失败，后续相同保存请求会先核对目标文件内容；当前实现不会把任意客户端路径作为恢复目标。

只读生成阶段失败时，可调用 `POST /api/writing-tasks/{task_id}/retry` 并携带失败任务版本；保存阶段失败不通过 retry 恢复，而是继续使用原幂等键重试。系统不承诺正在执行的任意工具自动恢复。真实 LLM、真实博客内容和人工写作质量验收另行执行。

## 任务 6 集成验证

跨模块集成测试使用独立的 `backend/data/rag/databases/writing-research-fixture-2026-09-26-<uuid>.db` 作为 RAG fixture，业务 Session、写作任务和 attempts 使用测试临时数据库；不写入 `agent.db` 或 `knowledge.db`，不访问真实模型。测试覆盖真实 keyword 检索、证据快照、提纲引用、重复执行、确认边界、空知识库、跨任务隔离、来源更新后的历史快照和错误后的重试边界；检索单测另外覆盖 hybrid 成功、缺失 Embedding 回退、索引配置不兼容回退和 Provider 错误透传。Fake LLM 的通过只证明调用边界与持久化链路，不代表真实模型质量验收通过。

验证命令：

```bash
cd backend
.venv/bin/python -m pytest tests/test_writing_research_integration.py -q
```

工程验证结果（2026-09-27）：完整执行 `backend/tests/` 下全部 `test_writing*.py`，共 `94 passed`；执行器测试 `14/14`；二次取消回归连续 `5/5`；`py_compile`、`git diff --check` 和前端 `npm run build` 通过。测试覆盖正常闭环（含用户编辑提纲、未确认边界、重复生成及幂等保存）、材料不足不调用模型/不落盘、草稿 Provider 失败不保存、运行中 attempt 冲突和二次取消清理。

机器报告位于 `backend/data/rag/reports/2026-09-27/writing-stage1/dev/machine-baseline.md`。真实样例使用 `gpt-5.6-luna` 和 `public-blog-2026-09-23-v1.db` 完成研究、草稿和保存，状态与引用校验通过；但 `Transformer架构` 的 keyword 样例召回 React Fiber 材料，人工主题覆盖不通过，报告位于 `backend/data/rag/reports/2026-09-27/writing-stage1/dev/507127c4579c4118b8c63cf60ccc24c9/real-model-quality-review.md`。修正后的 hybrid runner 实际使用 hybrid 且未回退，但因当前快照证据不足以 `evidence_insufficient` 停止，报告位于 `backend/data/rag/reports/2026-09-27/writing-stage1/dev/7adf911a9d3342208b00828b2d2d6e6c/hybrid-retrieval-acceptance.md`。因此工程机器验收通过，真实模型内容质量仍不通过；不宣称硬崩溃自动恢复、分布式执行、自动重试或公开多租户安全隔离。
