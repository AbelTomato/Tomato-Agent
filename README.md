# Tomato Agent

一个由使用者手动实现 Agent Runtime 的最小 Agent 基础设施项目。

## 重要边界

本项目**不实现 Agent Runtime 核心 Loop**，也不内置真实 LLM API。用户需要自行实现：用户输入处理、LLM 请求、直接回答或工具调用判断、工具结果判断、多轮 Loop 和最终回答生成。项目提供 Runtime 可调用的基础设施。

## 开发记录和代码审查责任

每轮开发日志必须保存会话过程中用户发送的每一条 Prompt 原文，不能用摘要替代。Agent 可以执行测试和自检并列出 CR 清单，但人工 Code Review 由项目使用者执行；在收到用户反馈前，日志中的人工 CR 状态为“待用户执行”。

## 技术栈

- Backend: FastAPI、Python、Pydantic、SQLite
- Frontend: React、TypeScript、Vite、Tailwind CSS
- 包管理: `pip`、`pnpm`

## 运行方式

### Backend

```powershell
cd D:\AbelTomato_Files\Developer\Projects\Tomato-Agent\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
uvicorn app.main:app --reload
```

API 默认地址：`http://127.0.0.1:8000`，文档：`/docs`。

### Frontend

```powershell
cd D:\AbelTomato_Files\Developer\Projects\Tomato-Agent\frontend
pnpm install
pnpm dev
```

## 系统设计

后端提供三类基础设施：

1. `SessionRepository`：保存 Session、Run、Event、Checkpoint，Session 间不共享对话状态。
2. `ContextManager`：组织系统指令、Session 摘要、Memory、未解决问题和最近消息，并在达到阈值时进行确定性压缩。
3. `ToolRegistry`：注册和执行 `calculator`、`search`、`read_docs`。

Runtime 应在每次 LLM 调用前调用 Context 管理器，在工具调用前通过 Registry 校验并执行工具，在关键步骤后写入 Event 和 Checkpoint。

## Memory 召回时机和放置方式

Memory 在新用户消息进入后、调用 LLM 前召回；当任务涉及历史事实、用户偏好或未完成任务时才召回，不在每个工具结果后无条件召回。

Memory 放在 Context 的 `<Relevant Memory>` 区块，位于 Session Summary 之后、Recent Conversation 之前，并标记为不可信数据，不能覆盖系统指令：

```text
<System Instructions>
<Session Summary>
<Relevant Memory>  # untrusted data, not instructions
<Unresolved Questions>
<Recent Conversation>
```

当前基础实现将结构化 Memory 放在 Session metadata 中；未引入向量数据库。后续可在不改变 Runtime 接口的情况下增加独立 Memory Repository。

## 目录

- `backend/app/agent`：Runtime 接口、Context 模型和 Memory 基础能力
- `backend/app/tools`：工具协议、Registry 和三个工具
- `backend/app/sessions`：SQLite 持久化
- `backend/app/observability`：日志和 Trace
- `backend/tests`：单元及集成测试
- `docs/development-log`：逐轮开发记录
