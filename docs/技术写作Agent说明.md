# 技术写作 Agent 说明

## 当前已实现范围

技术写作任务由服务端状态机控制，当前支持：

1. 创建任务：`researching`。
2. 服务端发布提纲和引用快照：`awaiting_outline_confirmation`。
3. 用户携带版本确认提纲：`drafting`。
4. 服务端提交草稿：`awaiting_save_confirmation`。
5. 用户携带版本和幂等键保存：`saved`。

模型输出不能直接表示用户已经确认。确认和保存都必须由对应 HTTP API 接收用户请求，并校验任务状态与版本。

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