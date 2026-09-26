# Tomato Agent

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
