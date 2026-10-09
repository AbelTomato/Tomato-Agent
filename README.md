# Tomato Agent

Tomato Agent 是一个面向个人知识库与技术写作的 Agent 项目，提供本地 Web 界面和 HTTP API，并包含实验性的受控代码任务能力。

## 主要功能

- **对话与工具调用**：保存会话，执行有预算和工具白名单约束的模型调用。
- **知识库检索**：导入 Markdown，支持关键词、向量和混合检索，并保留引用来源。
- **技术写作**：检索证据、生成提纲，经用户确认后生成并保存草稿。
- **受控代码任务（实验性）**：通过 HTTP API 在固定 fixture 上修复代码、运行隔离测试，输出报告与 diff。

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

当前按本地可信单用户场景使用。写作流程由用户显式推进；代码任务限定单进程、一次一个任务和固定 `python_calculator` fixture，需另行配置 Docker/OpenShell 隔离后端。硬崩溃恢复、多 Worker 与公开多租户安全尚未完成，已有测试与单次真实闭环不代表生产放行。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [本地开发指南](docs/本地开发指南.md) | 跨平台启动、环境配置与常见问题 |
| [知识库使用说明](docs/知识库使用说明.md) | Markdown 导入、索引与检索 |
| [技术写作 Agent 说明](docs/技术写作Agent说明.md) | 写作流程、API、验证与限制 |
| [受控代码任务说明](docs/受控代码任务说明.md) | 工具契约、API、隔离执行与验收边界 |
| [Benchmark 使用说明](docs/评测/Benchmark使用说明.md) | RAG 评测与可追溯报告 |
| [博客 RAG 基线报告](docs/评测/博客RAG基线报告.md) | 检索实验结果与质量限制 |
| [设计权衡与潜在问题台账](docs/设计权衡与潜在问题台账.md) | 当前技术债务、风险与重新评估条件 |
