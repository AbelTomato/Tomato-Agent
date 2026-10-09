# Tomato Agent

## 项目限制与设计权衡

当前接受的设计限制、技术债务、验证缺口及重新评估条件集中记录在[设计权衡与潜在问题台账](/home/abeltomato/workspace/projects/Tomato-Agent/docs/设计权衡与潜在问题台账.md)。台账维护当前状态，实施计划记录处理任务，开发日志保留过程和历史验收证据。

## 技术栈

- Backend: FastAPI、Python、Pydantic、SQLite
- Frontend: React、TypeScript、Vite、Tailwind CSS
- 包管理: Python `venv`/`pip`、`pnpm`

## 部署

### 1. 配置后端环境变量

```bash
cd backend
cp .env.example .env
```

Windows PowerShell：

```powershell
cd backend
Copy-Item .env.example .env
```

至少配置 `LLM_API_KEY` 才能执行真实模型调用；保持 Embedding 配置为空时，知识库只提供关键词检索

### 2. 启动后端

#### Linux / macOS

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m uvicorn app.main:app --reload
```

如果 Ubuntu/Debian 提示缺少 `venv` 模块，安装系统组件后重试：

```bash
sudo apt update
sudo apt install python3-venv python3-full
```

#### Windows PowerShell

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m uvicorn app.main:app --reload
```

如果 PowerShell 禁止执行激活脚本，可以不激活虚拟环境，直接调用其中的解释器：

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

#### Windows CMD

```bat
cd backend
py -3.11 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -e .
python -m uvicorn app.main:app --reload
```

### 3. 启动前端

```bash
cd frontend
pnpm install
pnpm dev
```

```bash
corepack enable
corepack prepare pnpm@latest --activate
```

## 受控代码任务契约

任务 1–9 的工程闭环已完成。Docker/OpenShell 真实 Adapter 已实现，基础隔离、超时和取消验收各 3/3 通过；完整资源/生命周期故障注入、后台 Worker 和多 Worker 恢复仍待独立验证。

服务端通过 `Settings.code_task_budget` 和 `Settings.code_task_capability_profile(workspace_root)` 生成预算与能力配置。预算字段与 Harness 保持同名；`CODE_TASK_MAX_LOOPS`、`CODE_TASK_MAX_TOOL_CALLS`、`CODE_TASK_MAX_DURATION_SECONDS`、`CODE_TASK_MAX_CONTEXT_TOKENS`、`CODE_TASK_MAX_RESPONSE_CHARS` 控制执行预算；`CODE_TASK_TOOL_TIMEOUT_SECONDS`、`CODE_TASK_MAX_TOOL_RESULT_CHARS`、`CODE_TASK_MAX_FILE_BYTES`、`CODE_TASK_MAX_ARTIFACT_BYTES` 分别声明工具超时、输出、单文件和产物大小限制，默认值见 `backend/.env.example`。workspace 与 artifact 根目录由 `CODE_TASK_WORKSPACE_ROOT`、`CODE_TASK_ARTIFACT_ROOT` 配置，文件、产物和输出限制由对应服务执行。

网络、凭据及进程能力保持关闭，无任意 shell 工具。代码任务配置允许计划中的八个工具名称，其中七个 workspace 工具通过 `create_code_workspace_registry(workspaces, artifacts, runs)` 显式注册，普通 Session 不自动获得这些工具。工具检查服务端 `ToolContext` 的 Run/workspace 归属、profile 工具白名单及允许路径；缺失授权时拒绝执行。既有 Sandbox 后端不可用时仍默认拒绝，契约单测通过不代表真实隔离安全验收通过。

workspace 工具为 `list_files`、`read_file`、`search_files`、`write_file`、`apply_patch`、`get_diff`、`collect_artifact`，只接受 workspace 内相对路径。`search_files` 按字面字符串匹配；`apply_patch` 接受 `{"changes":[{"path":"src/main.py","old_text":"old","new_text":"new"}]}`，原文必须恰好匹配一次，不接受 shell 或自由文本补丁。整批补丁先校验路径、匹配和大小再写入；文件系统错误造成的中途写入失败不提供事务回滚。结果以 `truncated` 标记截断；`get_diff` 同时保存完整 diff 产物并返回引用。文件大小由 WorkspaceService、产物大小由 ArtifactService 执行限制，输出上限由服务端 profile 控制。

任务 5 验证命令（在后端目录执行）：`.venv/bin/python -m pytest tests/test_code_workspace_tools.py tests/test_tools.py tests/test_harness_policies.py -q`。

`RunTestsTool` 通过显式注入 `TestTargetRegistry` 和 `SandboxExecutor` 使用，输入仅接受 `{"target":"unit","arguments":["-q"]}`。服务端 `RegisteredTestTarget` 固定 argv 命令模板、workspace 相对工作目录和逐项允许参数；未知目标、未登记参数及客户端 command/shell/cwd/env/profile 均被拒绝。工具不执行宿主机命令，默认 Sandbox 后端不可用时返回 `backend_unavailable`。现有进程能力仍关闭，真实隔离执行需后续独立后端适配。

测试报告包含执行状态、退出码、`passed`、输出截断标记和错误码，并保存为 Run 归属的 `test_report` ArtifactRef 与 workspace metadata 引用。Sandbox `completed` 表示执行结束，只有退出码为 0 才标记测试通过；非零或缺失退出码保留为测试未通过。超时、取消、策略拒绝和执行异常返回失败及报告引用。报告保存受 ArtifactService 大小限制，无法保存时不会返回成功。代码任务编排统一返回 usage、tool calls、events、artifacts、test results、diff artifact 和 changed files；普通 Session 不自动获得代码工具。

任务 6 验证命令（在后端目录执行）：`.venv/bin/python -m pytest tests/test_code_tests_tool.py tests/test_sandbox.py tests/test_runtime.py -q`。当前证据仅来自 fake backend 和契约测试，不代表真实隔离后端验收通过。

代码任务 API 包含 `POST /api/code-tasks`、`POST /api/code-tasks/{run_id}/execute`、`GET /api/code-tasks/{run_id}`、`GET /api/code-tasks/{run_id}/events`、`POST /api/code-tasks/{run_id}/cancel` 和 `GET /api/code-tasks/{run_id}/artifacts/{artifact_id}`。创建后 Run 进入 `running`；未配置模型时 execute 返回 `503 model_unconfigured`，不会伪造成功。artifact 读取会校验 Run 归属。任务 API 通过应用 lifespan 初始化独立 workspace/artifact 存储，不改变 Session、Knowledge 和 Writing 路由。

任务 8 验证命令（在后端目录执行）：`.venv/bin/python -m pytest tests/test_code_tasks_api.py tests/test_application.py tests/test_main.py tests/test_writing_api.py -q`。

任务 8 的 API 回归还覆盖固定 `python_calculator` fixture 的受控 patch、`unit` 测试目标、diff/artifact 读取、测试失败、重复执行、未知 Run、非法输入、取消幂等和跨 Run artifact 越权。默认应用未配置模型时仍返回 `503 model_unconfigured`；真实 Sandbox backend 不可用时保持 failure-closed。当前 HTTP API 的取消验证覆盖未开始或终态 Run，正在执行任务的后台中断与恢复属于后续 Worker 生命周期工作。

普通测试非零或缺失退出码作为可修复反馈返回模型，Run 保持运行；模型可在既有循环、工具调用和时间预算内修复并重新测试，每次重测单独消耗工具调用预算，不在工具内部自动重复同一测试。最终测试仍失败时返回 `tests_failed`，预算耗尽时返回 `budget_exhausted`。路径拒绝、未知测试目标、后端不可用、超时和取消立即终止。成功须由程序校验最新测试通过、diff 与测试时的 workspace 一致、修改范围合法及产物可读，模型文本不能覆盖失败。

任务 9 端到端验证使用内容感知 fake backend：只检查固定 calculator fixture 的 AST 与测试文本，模拟修复前退出码 1 和修复后退出码 0，不执行生成代码或命令。HTTP 闭环核验事件连续顺序、独立 Run 归属、只修改 `calculator.py`、测试报告和 diff 可读，以及 usage 存在；usage 中的零 token 不代表真实 Provider 费用测量。应用首期固定使用 `python_calculator` 输入及服务端 `unit` 目标，不接受客户端仓库路径或命令。

```bash
cd /home/abeltomato/workspace/projects/Tomato-Agent/backend
.venv/bin/python -m pytest tests/test_code_task_integration.py -q
```

任务 1 验证命令：

```bash
cd /home/abeltomato/workspace/projects/Tomato-Agent/backend
.venv/bin/python -m pytest tests/test_code_task_contracts.py tests/test_harness_models.py tests/test_harness_policies.py tests/test_sandbox.py -q
.venv/bin/python -m compileall -q app tests
```

### 受控代码任务最小 MVP 操作边界

2026-10-09 已完成真实 `gpt-5.6-luna` + OpenShell 的 HTTP 成功闭环：unit 先失败再通过，仅 calculator.py 改动，Run completed，8 次模型/7 次工具调用，报告/diff 可读且事件连续，验收后 Sandbox 列表为空。此前一次失败证据保留，尚未完成多次稳定性与真实 API 故障矩阵，不等同生产放行。

限定本机可信单用户、单进程、一次一个任务、固定 `python_calculator` fixture，通过 HTTP API 使用。使用已有 Provider 与固定 digest Sandbox 镜像，后端绑定 `127.0.0.1`。OpenShell 建议起步设置 `CODE_TASK_TOOL_TIMEOUT_SECONDS=60`、`CODE_TASK_MAX_DURATION_SECONDS=300`、`CODE_TASK_MAX_LOOPS=20`、`CODE_TASK_MAX_TOOL_CALLS=16`；创建和上传也计入工具期限。Gateway 重建后核对 supervisor 回连地址。

创建后调用 execute（同步等待），再查询 Run、events 和 artifacts。运行中 cancel 会取消进程内执行协程并等待执行器清理；重复取消不会重复中断清理。硬崩溃不自动恢复：人工核对遗留 Run 和 Sandbox 所有权标签，不盲目重放 execute 或批量删除其他任务资源。

真实 HTTP 验收：在 `/home/abeltomato/workspace/projects/Tomato-Agent/backend` 执行 `RUN_CODE_TASK_ACCEPTANCE=1 .venv/bin/python -m pytest tests/test_code_task_acceptance.py -q -s`。该命令调用已配置模型并创建/清理 Sandbox，产生模型费用；使用独立数据库和 workspace，产物保存到 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/data/code-task-acceptance/<运行标识>/`。未启用时跳过，不计通过。Harness usage 当前记录模型调用数，Token 字段尚未汇总真实 Provider 用量，不能用于费用结论。

## 技术写作任务执行器

技术写作采用用户显式推进的状态机：创建任务 → 开始研究 → 确认提纲 → 生成草稿 → 使用幂等键保存。服务端固定任务版本并保存完整证据快照，未知引用、额外控制字段、无证据和非法模型输出会被拒绝；未确认提纲不会生成草稿或保存文件。研究默认使用 `hybrid`，仅在已知 Embedding/索引不可用时记录原因并回退 `keyword`，Provider 网络或认证错误不会静默回退。

该入口是带超时的同步显式调用，不是持久化队列或后台 Worker。页面刷新不会自动重放生成；运行中 attempt 不自动接管，硬崩溃恢复、分布式执行、自动重试和公开多租户鉴权不在当前范围内。RAG 快照和实验报告分别写入 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/data/rag/databases/` 与 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/data/rag/reports/`。

完整写作测试集：

```bash
cd /home/abeltomato/workspace/projects/Tomato-Agent/backend
files=$(find tests -maxdepth 1 -type f -name 'test_writing*.py' -print | sort)
.venv/bin/python -m pytest $files -q
```

机器测试通过不代表真实模型主题覆盖或人工内容质量通过；真实模型报告和限制见 [`docs/技术写作Agent说明.md`](docs/技术写作Agent说明.md)。

## 知识库评测

知识库导入和评测使用固定的 JSON 清单与 JSONL 问题集。评测命令要求显式提供已导入的 SQLite 知识库：

```bash
cd backend
.venv/bin/python -m app.knowledge.evaluation \
  --dataset /absolute/path/to/questions.jsonl \
  --split dev \
  --mode keyword \
  --output /home/abeltomato/workspace/projects/Tomato-Agent/backend/data/rag/reports/YYYY-MM-DD/<dataset>/dev/<run-name>/report.json \
  --database /home/abeltomato/workspace/projects/Tomato-Agent/backend/data/rag/databases/<snapshot>.db
```

`vector` 和 `hybrid` 模式还需要兼容的 Embedding 模型、维度和索引。评测可记录证据段召回/排名、空结果、候选与最终证据状态及阶段耗时；这些机器指标不代表答案语义质量。模型答案质量和真实博客效果仍须人工验收，不能用固定测试语料单测成绩代替。RAG 快照和报告分别保存在上述 `databases/`、`reports/` 目录中；当前结果与限制见 [`docs/评测/博客RAG基线报告.md`](docs/评测/博客RAG基线报告.md)。

可追溯的 run/compare Benchmark runner 使用正式 manifest 执行指定 split，并输出逐题 JSON 与 Markdown 报告：

```bash
cd backend
.venv/bin/python -m app.knowledge.benchmark run \
  --manifest /home/abeltomato/workspace/projects/Tomato-Agent/backend/evals/benchmark_manifest.json \
  --strategy keyword-v1 --split dev \
  --output /home/abeltomato/workspace/projects/Tomato-Agent/backend/data/rag/reports/YYYY-MM-DD/public-blog/dev/benchmark-v1/keyword-v1
```

使用前请阅读 [`docs/评测/Benchmark使用说明.md`](docs/评测/Benchmark使用说明.md)。输出目录必须不存在；真实运行仅使用获批的只读 RAG 快照和题集 split。`complete` 只代表运行完整，不代表答案语义或产品质量验收通过。
