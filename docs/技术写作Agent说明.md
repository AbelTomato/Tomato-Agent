# 技术写作 Agent 说明

## 当前已实现范围

技术写作任务由服务端状态机控制，当前支持：

1. 创建任务：`researching`。
2. 服务端发布提纲和引用快照：`awaiting_outline_confirmation`。
3. 用户携带版本确认提纲：`drafting`。
4. 服务端提交草稿：`awaiting_save_confirmation`。
5. 用户携带版本和幂等键保存：`saved`。

模型输出不能直接表示用户已经确认。确认和保存都必须由对应 HTTP API 接收用户请求，并校验任务状态与版本。

首期研究执行器已经覆盖“检索证据 → 生成提纲 → 校验引用 → 原子发布”的切片，但边界停在人工确认前。创建任务不会自动执行，也没有持久化队列或后台 Worker。

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

该接口只返回 attempt 的阶段、状态、输入版本、Run ID、配置限制、模型标识和稳定错误码。重复提交已成功的相同输入版本返回当前任务，不再次调用模型；`running`、版本冲突或过期结果返回 `409`。进程硬退出留下的 `running` 记录不会自动接管。

## 研究限制与错误

默认 `retrieval_mode=keyword`，适合英文 token（例如 `SETEX`）命中；中文主题召回不足时返回 `422` 的 `evidence_insufficient`，不会为了演示成功偷偷切换检索模式。`vector`/`hybrid` 必须有完整 Embedding 配置，缺失时失败而不降级。空证据不会调用提纲模型。

常见错误映射如下：

| HTTP | `detail.code` |
| --- | --- |
| 422 | `evidence_insufficient`、`context_budget_exceeded` |
| 502 | `retrieval_failed`、`provider_failed`、`invalid_model_response`、`invalid_citation` |
| 503 | `model_unconfigured`、`publication_failed`、`storage_unavailable` |
| 504 | `deadline_exceeded` |

模型只能返回结构化提纲和服务端证据 ID；服务端不信任模型提供的路径、行号或正文。通过校验不代表语义支撑已经人工验收。研究 Run 是独立阶段记录，不写入普通聊天消息，也不复用其他 Session 的历史上下文。

### 确认提纲

```http
POST /api/writing-tasks/{task_id}/confirm-outline
Content-Type: application/json

{"version":2,"outline":{"sections":["过期策略","常见陷阱"]}}
```

版本过期、状态不匹配或并发更新返回 `409`，不会覆盖当前任务。

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

跨模块集成测试使用独立的 `backend/data/rag/databases/writing-research-fixture-2026-09-26-<uuid>.db` 作为 RAG fixture，业务 Session、写作任务和 attempts 使用测试临时数据库；不写入 `agent.db` 或 `knowledge.db`，不访问真实模型。测试覆盖真实 keyword 检索、证据快照、提纲引用、重复执行、确认边界、空知识库、跨任务隔离、来源更新后的历史快照和错误后的重试边界。Fake LLM 的通过只证明调用边界与持久化链路，不代表真实模型质量验收通过。

验证命令：

```bash
cd backend
.venv/bin/python -m pytest tests/test_writing_research_integration.py -q
```

本轮实际验证结果：集成测试 `6/6` 通过；执行器、仓储、研究与集成直接回归 `34/34` 通过；API、主程序与写作工作流回归 `25/25` 通过。真实模型、真实生产快照和人工语义质量验收仍未执行。上述测试覆盖取消登记所有权、publication 的 task/attempt/Run 关联核验、未知引用拒绝和不同任务证据隔离，但不承诺进程硬崩溃后的自动恢复、分布式执行、自动重试或公开多租户安全隔离。