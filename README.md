# Tomato Agent

Tomato Agent 是一个面向个人知识库与技术写作的 Agent 项目，提供本地 Web 界面和 HTTP API，并包含实验性的受控代码任务能力。

## 主要功能

- **对话与工具调用**：保存会话，执行有预算和工具白名单约束的模型调用。
- **知识库检索**：导入 Markdown，支持关键词、向量和混合检索，并保留引用来源。
- **技术写作**：检索证据、生成提纲，经用户确认后生成并保存草稿。
- **受控代码任务（实验性）**：通过 HTTP API 在固定 fixture 上修复代码、运行隔离测试，输出报告与 diff。
- **代码任务操作面**：Web 页面提供提交、状态与事件轮询、取消、人工恢复确认及测试报告/Artifact/diff 查看；已通过本机 Chrome 与真实 Provider/OpenShell 成功闭环、running 主动取消、刷新恢复和终态停止轮询验收。waiting 检查与确认使用独立未知副作用夹具通过真实 API/浏览器验证，未知事实继续保持等待。

## 技术栈

- 后端：Python ≥ 3.11、FastAPI、Pydantic、SQLite。
- 前端：React、TypeScript、Vite、Tailwind CSS。
- 包管理：Python `venv` / `pip`、pnpm 10.34.5。

## 快速开始

以下命令适用于 Linux / macOS。准备 Python ≥ 3.11、与 Vite 兼容的 Node.js 和 pnpm；Windows 步骤与环境排障见[本地开发指南](docs/本地开发指南.md)。

### 1. 配置并启动后端

首次配置时，在项目根目录打开第一个终端（已有 `.env` 时跳过复制步骤）：

```bash
cd backend
cp .env.example .env
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

编辑 `backend/.env`，配置 `LLM_API_KEY`、`LLM_BASE_URL` 和 `LLM_MODEL`，然后在同一终端启动：

```bash
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

真实模型调用需要有效的 Provider 配置。Embedding 配置为空时，知识库仅提供关键词检索；导入语料与建立向量索引见[知识库使用说明](docs/知识库使用说明.md)。请勿提交含密钥的 `.env`。

### 2. 启动前端

在项目根目录打开第二个终端：

```bash
cd frontend
pnpm install
pnpm dev
```

- Web 界面：<http://localhost:5173>
- API 文档：<http://127.0.0.1:8000/docs>
- 后端健康检查：<http://127.0.0.1:8000/health>

## 当前使用边界

当前按本地可信单用户、一次一个任务场景使用。写作流程由用户显式推进；代码任务限定固定 `python_calculator` fixture，需另行配置 Docker/OpenShell 隔离后端。设置 `CODE_TASK_WORKER_ENABLED=true` 后，应用启动会先初始化执行仓储、处理安全恢复，再由单机单 Worker 自动领取 queued Run；关闭时会停止 Worker。创建/查询响应包含 `dispatch_status`，禁用时明确为 `worker_disabled`。SQLite 执行内核已通过 fake LLM/工具的独立进程硬崩溃、显式恢复、竞争领取与旧执行者 fencing 验证；未知写入/Sandbox 副作用进入等待，不盲目重放。当前后台能力已在代码任务范围内完成单 Worker 交付验收，但写作尚未接入该恢复内核；多 Worker、背压/限流、生产规模、真实 Gateway 断连与资源故障矩阵、公开多租户安全仍未验收，测试与单次真实闭环不代表生产放行。

## 文档导航

统一会话与原目录代码任务 MVP 计划任务 1～6 已完成（2026-10-11）：公共 Run、原目录注册、分轮 diff/冲突、服务层 pytest、四类适配器/单 Worker 分派及统一 API/只读历史已实现。任务 6 直接影响 14 文件回归 194 passed（无失败/跳过）；覆盖 HTTP 确认/保存重放、游标分页、legacy 历史去重、默认工作区持久化和显式原目录绑定。新入口为 `POST /api/sessions/{id}/tasks`，旧 Web/代码任务 HTTP 入口仍使用 fixture；统一前端及真实原目录闭环留任务 7～8。`TASK_WORKER_ENABLED` 显式优先，未设置回退 `CODE_TASK_WORKER_ENABLED`，只启动一个 Worker。聊天/RAG/写作仅阶段恢复，未知保存保持等待；原目录服务限定 Python/pytest、digest 固定 Docker，不代表完整 MVP 交付，详见[实施计划](docs/实施计划/2026.10.11-统一会话与原目录代码任务MVP实施计划.md)及[受控代码任务说明](docs/受控代码任务说明.md)。

后台执行计划任务 1～6 已完成功能与交付门禁（2026-10-11，未提交 Git）。最终后端目标回归 133 passed、6 个真实 opt-in 场景跳过；聊天/知识/写作目标回归 33 passed，前端 5/5 与 TypeScript/Vite 构建通过。任务 5 已有 Docker 五场景、OpenShell 五场景及后台 HTTP/独立进程六场景通过证据，本轮核实并复用；跳过不计通过。后台故障验收使用脚本模型，清理失败采用显式注入；真实 Sandbox 未知副作用重启保持 waiting。工具集合序列化顺序已固定，旧 Run 摘要不自动迁移。具体命令、资源检查和未验收边界见[受控代码任务说明](docs/受控代码任务说明.md)。

独立 `ExecutionWorker` 协调器负责单并发领取、租约心跳、启动恢复和关闭等待。代码任务创建和 execute 请求返回 `202`；前端提供状态/事件轮询、取消、人工恢复确认和 artifact/diff 查看。该能力仍限代码任务、单机单 Worker 和固定 fixture，不代表写作或其他业务已接入同一后台执行内核。

| 文档 | 内容 |
| --- | --- |
| [本地开发指南](docs/本地开发指南.md) | 跨平台启动、环境配置与常见问题 |
| [知识库使用说明](docs/知识库使用说明.md) | Markdown 导入、索引与检索 |
| [技术写作 Agent 说明](docs/技术写作Agent说明.md) | 写作流程、API、验证与限制 |
| [受控代码任务说明](docs/受控代码任务说明.md) | 工具契约、API、隔离执行与验收边界 |
| [Benchmark 使用说明](docs/评测/Benchmark使用说明.md) | RAG 评测与可追溯报告 |
| [博客 RAG 基线报告](docs/评测/博客RAG基线报告.md) | 检索实验结果与质量限制 |
| [设计权衡与潜在问题台账](docs/设计权衡与潜在问题台账.md) | 当前技术债务、风险与重新评估条件 |
