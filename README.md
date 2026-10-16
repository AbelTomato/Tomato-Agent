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

## 验证入口

统一验证入口为 `python3 scripts/verify.py`，每次执行会在 `verification-artifacts/<run-id>/` 写入 `result.json` 和各步骤日志。常用命令：

```bash
python3 scripts/verify.py quick --static --backend-test tests/test_assistant_api.py --frontend-test tests/assistant-result.test.mjs
python3 scripts/verify.py module assistant execution rag writing frontend
python3 scripts/verify.py integration
```

`quick` 接受重复的 `--backend-test` / `--frontend-test` 指定目标；`--static` 执行 Python compileall 和前端 TypeScript/Vite 构建。模块名为 `assistant`、`execution`、`rag`、`writing`、`frontend`。集成门禁使用独立 pytest 临时目录，覆盖 SQLite/Worker 生命周期、API 流程和前端测试。CI 常规门禁执行模块回归及集成验证。

真实依赖验收不会被普通门禁调用，必须显式确认：

```bash
python3 scripts/verify.py external docker --confirm-real-dependencies
python3 scripts/verify.py external openshell --confirm-real-dependencies
python3 scripts/verify.py external provider --confirm-real-dependencies --provider-max-runs 4 --provider-max-requests 16 --external-timeout 600
```

Provider 当前验收预算上限为 4 个 Run、16 次请求（单 Run 最长 600 秒），可通过参数降低，并要求配置真实 Provider 及 Chrome CDP；真实请求可能产生费用，必须显式 opt-in，保持为 CI 外部步骤。离线 replay 使用录制响应且不调用真实 Provider，只验证离线解析和 UI/Runtime 流程，不产生真实兼容性资格结论。真实 Provider 结论仅对报告记录的验收日期、Provider、模型、端点主机及有效请求配置和实际通过场景成立，不外推到其他配置。Docker/OpenShell 要求本机对应运行环境与镜像配置。各层结果互相独立，低层通过不代表更高层通过。操作、预算、隔离和报告字段见[真实 Provider 本地 UI 验收说明](docs/真实Provider本地UI验收.md)。

## 快速开始

验证环境、CI 触发方式及当前覆盖边界见[分层验证与 CI 说明](docs/分层验证与CI说明.md)。

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
- 统一助手 Runtime 初版采用严格的协议版本 1 决策，并仅对 Runtime 决策请求启用 Provider strict `json_schema` Structured Outputs；本地 Pydantic 校验及权限/完成门禁仍保留。当前已完成离线 schema 与 fake Provider 工程验证，真实应用级 Provider 兼容验收仍待单独执行。`message` 是可连续出现的阶段消息，只有 `finish` 能完成 Run；`ask_user` 会进入持久化等待，工具发现/描述由服务端目录提供，`call_tool` 仍经服务端权限和契约校验。助手不把旧式 Provider 裸文本或工具调用自动当成协议决策；共享 Harness、代码任务、知识与写作消费者仍使用旧 `LLMResponse`。不提供 SSE/WebSocket 或实时事件 UI。
- 统一助手内核已完成任务 1～4：公共契约、快照/共享模型预算、Harness 等待/失败反馈，以及知识与项目只读 HTTP/Worker 闭环。实验性开关 `[assistant].enabled=true` 与 `[worker].enabled=true` 配合有效模型后，可向统一任务 API 提交 `kind=assistant`；默认关闭并返回 `503 assistant_disabled`。同一 Run 支持检索、知识原文/有证据回答、项目列表/搜索/读取与澄清；缺工作区通过版本化服务端动作绑定已注册且获准目录一次，拒绝后不能换工具绕过。顶层和领域模型调用共用持久化 ledger，引用按已观察证据校验，只读结果不宣称测试通过。现有四类模式兼容；前端默认助手入口已实现，其他四类入口可通过高级模式选择；工作区可选，等待卡片可补充说明、绑定已注册项目或拒绝请求。禁用时保留输入并提示开启配置，不自动降级。首期真实模型/浏览器验收曾在授权的 3 Run（累计 8 次调用）中发现跨能力回答超时以及澄清/绑定未产生等待动作；这是早期助手内核验收记录，之后已完成独立的 Runtime 决策与事件协议工程实现及目标测试，不应与本页 Runtime 初版状态混为一谈。任务 4 的 342 项回归使用 fake Provider/独立测试库，不代表真实模型验收。配置见[环境配置说明](docs/环境配置说明.md)，范围和证据见[第一期实施计划](docs/实施计划/2026.10.11-统一助手内核第一期实施计划.md)。

## 文档导航

2026-10-14 Docker `run_tests` 终止验证计划任务 1～5 已完成工程阶段，包含服务端 Docker verifier 接线与 settlement 证据门禁；真实 Docker 已覆盖隔离、失败、创建失败、超时、取消、daemon 重启和应用重启查询等记录在[Docker 终止验证实施计划](docs/实施计划/2026.10.14-Docker%20run_tests生产级终止验证实施计划.md)中。此处保留为独立子系统的历史验收范围，不扩展为统一助手 Runtime 初版的发布条件或继续开发任务。

统一助手 Runtime 初版范围和发布决策：Runtime 决策协议与事件协议计划任务 1～7 已完成工程验收，范围、目标测试及历史记录见[Runtime 实施计划](docs/实施计划/2026.10.12-统一助手Runtime决策与事件协议实施计划.md)。本初版冻结当前实现，不再开展副作用相关实现或专项验证；未知副作用仍按当前实现进入等待/核查与隔离，风险接受仅影响 admission，不证明操作终止、不解除隔离、不生成 termination evidence。Docker `run_tests` 已有其服务端 verifier；该实现和已有测试作为现状保留，不扩展到 OpenShell 或其他通用副作用，也不再作为 Runtime 初版的待办门槛。Strict JSON Schema 属于本次批准的冻结范围后增量，仅用于 Runtime 决策调用；Provider 资格验收与运行时错误行为需单独验证，不增加 SSE/WebSocket 或实时事件 UI，也不改变本项目当前仅面向本地可信单用户的使用边界；助手功能仍由配置开关控制，默认关闭。

Runtime 决策与事件协议工程任务 1～7 已完成验收。阶段消息以 `assistant.message` 追加到既有 RunEvent；工具目录提供版本、可用性与前置条件，调用启动前校验当前契约、参数和权限，旧版本仅允许服务端显式无损迁移。`tool.started/succeeded/failed/waiting/unknown` 关联同一操作与执行 Step，和操作事实事务提交；启动前等待/拒绝不会显示执行中，副作用异常保持未知并等待核查。事件使用 RuntimeEvent v1、固定摘要和最小详情，仅表明本地观察，不证明外部副作用。既有 `GET /api/tasks/{run_id}/events?after=...&limit=...` 返回有序事件及 `next_cursor`，外层响应和旧事件保持兼容。已记录任务 4 回归 226 项、共享执行补充 57 项通过，任务 6 完成门禁与 Run 集成目标测试 26 项通过。统一助手 Runtime 初版按当前实现冻结，不再开展副作用相关实现或专项验证；Docker `run_tests` verifier 及现有测试保留，不扩展其他通用副作用。未知操作仍按现有逻辑等待/核查与隔离，风险接受不构成终止证明。真实 Provider 兼容资格不在本初版验收结论内，当前不提供实时事件 UI。详见[Runtime 实施计划](docs/实施计划/2026.10.12-统一助手Runtime决策与事件协议实施计划.md)。

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
