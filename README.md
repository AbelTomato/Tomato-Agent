# Tomato Agent

## 技术栈

- Backend: FastAPI、Python、Pydantic、SQLite
- Frontend: React、TypeScript、Vite、Tailwind CSS
- 包管理: `pip`、`pnpm`

## 运行方式

### Backend

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
uvicorn app.main:app --reload
```

API 默认地址：`http://127.0.0.1:8000`，文档：`/docs`。

### Frontend

```powershell
cd frontend
pnpm install
pnpm dev
```

## 系统设计

后端提供 Agent Runtime 及其基础设施：

1. `SessionRepository`：保存 Session、Run、Event、Checkpoint，Session 间不共享对话状态。
2. `ContextManager`：组织系统指令、Session 摘要、Memory、未解决问题和最近消息，并在达到阈值时进行确定性压缩。
3. `ToolRegistry`：注册和执行 `calculator`、`search`、`read_docs`、`search_knowledge`、`read_knowledge` 五个工具。
4. `AgentRuntime`：执行受预算约束的 LLM/工具 Loop，并持久化 Run、Event 和 Checkpoint。

真实 LLM 通过 `LLMClient` 接口注入。未注入真实客户端时，HTTP Run 会返回失败结果，不会伪造模型答案。

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
- `backend/app/tools`：工具协议、Registry、基础工具及知识库搜索/读取工具
- `backend/app/knowledge`：知识库导入、索引、检索、问答服务与离线评测
- `backend/app/sessions`：SQLite 持久化
- `backend/app/observability`：日志和 Trace
- `backend/tests`：单元及集成测试
- `docs/development-log`：逐轮开发记录

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
