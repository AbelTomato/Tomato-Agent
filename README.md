# Tomato Agent

Tomato Agent 是一个面向个人知识库与技术写作的 Agent 项目，提供本地 Web 界面和 HTTP API，并包含实验性的受控代码任务能力。

## 主要功能

- **对话与工具调用**：保存会话，执行有预算和工具白名单约束的模型调用。
- **知识库检索**：导入 Markdown，支持关键词、向量和混合检索，并保留引用来源。
- **技术写作**：检索证据、生成提纲，经用户确认后生成并保存草稿。
- **统一会话（实验性 MVP）**：一个会话列表、时间线和输入框支持普通聊天、知识问答、技术写作与代码任务，展示引用、确认动作、测试报告与本轮 diff。
- **受控代码任务（实验性）**：注册原目录 Python 项目，直接修改并通过固定 Docker 镜像执行只读 pytest；支持状态查询、取消、人工恢复确认及测试报告、产物和 diff 查看。

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
cp config.example.toml config.local.toml
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

编辑 `backend/.env`，配置 `LLM_API_KEY`、`LLM_BASE_URL` 和 `LLM_MODEL`；运行策略放在 `backend/config.local.toml`，示例已开启统一任务所需 Worker。已有文件不要覆盖，旧 `.env` 项仍优先于 TOML，迁移规则见[环境配置说明](docs/环境配置说明.md)。然后在同一终端启动：

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

- 面向本地可信单用户、一次一个任务场景，尚不适用于公开多租户或生产规模部署。
- 写作研究自动运行，提纲与草稿保存由用户确认；聊天、知识问答和写作仅支持阶段恢复。
- 代码任务限定已注册的原目录 Python 项目、预置依赖与固定 Docker 镜像中的只读 pytest；取消任务不会回滚已经写入的内容。
- 无法确认是否完成的写入或执行操作会保持等待，需要人工核对后恢复。具体配置与限制见[受控代码任务说明](docs/受控代码任务说明.md)。
- 统一助手 Runtime 使用严格的协议版本 1 决策：`message` 是可连续出现的阶段消息，只有 `finish` 能完成 Run；`ask_user` 会进入持久化等待，工具发现/描述由服务端目录提供，`call_tool` 仍经服务端权限和契约校验。助手不再把旧式 Provider 裸文本或工具调用自动当成协议决策；共享 Harness、代码任务、知识与写作消费者暂时仍使用旧 `LLMResponse`，其全局迁移和 Provider 原生结构化输出验收另行进行。当前不提供 SSE/WebSocket 或实时事件 UI。
- 统一助手内核已完成任务 1～4：公共契约、快照/共享模型预算、Harness 等待/失败反馈，以及知识与项目只读 HTTP/Worker 闭环。实验性开关 `[assistant].enabled=true` 与 `[worker].enabled=true` 配合有效模型后，可向统一任务 API 提交 `kind=assistant`；默认关闭并返回 `503 assistant_disabled`。同一 Run 支持检索、知识原文/有证据回答、项目列表/搜索/读取与澄清；缺工作区通过版本化服务端动作绑定已注册且获准目录一次，拒绝后不能换工具绕过。顶层和领域模型调用共用持久化 ledger，引用按已观察证据校验，只读结果不宣称测试通过。现有四类模式兼容；前端默认助手入口已实现，其他四类入口可通过高级模式选择；工作区可选，等待卡片可补充说明、绑定已注册项目或拒绝请求。禁用时保留输入并提示开启配置，不自动降级。任务 5 前端 21 项测试与 TypeScript 检查通过、后端相关 27 项通过；真实模型/浏览器已在授权的 3 Run 范围执行（累计 8 次调用）：跨能力最终回答超时，澄清/绑定请求仅返回最终文本，任务 5 真实验收未通过，待修复和重新授权验收。项目写入/验证与写作留任务 6～7。任务 4 直接影响回归 342 passed、0 failed/skipped，仅 fake Provider/独立测试库，未验收真实模型行为。配置见[环境配置说明](docs/环境配置说明.md)，范围和证据见[第一期实施计划](docs/实施计划/2026.10.11-统一助手内核第一期实施计划.md)。

## 文档导航

2026-10-13 Runtime 决策与事件协议计划任务 1～3 已完成工程验收。阶段消息以 `assistant.message` 追加到既有 RunEvent，payload 为 RuntimeEvent v1（身份、时间、`decision-N` 关联、固定摘要和筛选后的 `detail.data.text`）；事件与检查点原子提交，重复提交冲突不会新增消息。既有 `GET /api/tasks/{run_id}/events?after=...&limit=...` 返回有序事件及 `next_cursor`，外层响应和旧事件保持兼容。任务 3 持久化/Harness/API/执行恢复 100 项、契约/策略/集成补充 95 项通过。助手顶层严格解析协议 v1 JSON，知识领域子调用继续旧契约；工具状态事件、完整回答形式校验和真实 Provider 资格验收仍待后续，当前不提供实时事件 UI，详见[Runtime 实施计划](docs/实施计划/2026.10.12-统一助手Runtime决策与事件协议实施计划.md)。

| 文档 | 内容 |
| --- | --- |
| [本地开发指南](docs/本地开发指南.md) | 跨平台启动、环境配置与常见问题 |
| [环境配置说明](docs/环境配置说明.md) | 精简 .env、TOML 配置项、优先级与旧配置迁移 |
| [知识库使用说明](docs/知识库使用说明.md) | Markdown 导入、索引与检索 |
| [技术写作 Agent 说明](docs/技术写作Agent说明.md) | 写作流程、API、验证与限制 |
| [受控代码任务说明](docs/受控代码任务说明.md) | 工具契约、API、隔离执行与验收边界 |
| [Benchmark 使用说明](docs/评测/Benchmark使用说明.md) | RAG 评测与可追溯报告 |
| [博客 RAG 基线报告](docs/评测/博客RAG基线报告.md) | 检索实验结果与质量限制 |
| [设计权衡与潜在问题台账](docs/设计权衡与潜在问题台账.md) | 当前技术债务、风险与重新评估条件 |
