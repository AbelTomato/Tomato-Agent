# RAG Benchmark 使用说明

## 范围与安全边界

Benchmark runner 仅评估检索，不生成答案，也不执行语义拒答评估。`complete` 表示题目运行和报告工件完整，不表示 RAG 产品质量通过。真实数据只允许使用已确认的知识库隔离快照；runner 以只读模式访问数据库，不初始化或迁移数据库。禁止把业务库 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/data/agent.db` 或 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/data/knowledge.db` 作为实验快照。

正式数据使用 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/evals/blog_questions.jsonl`。先核验 `dev`，不得为了调参读取 `test`；查看保留集后若继续调参，必须将其标记为不再独立。无答案检索为空只是拒答代理指标，不代表语义拒答正确。

所有数据库快照存放在 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/data/rag/databases/`；报告存放在 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/data/rag/reports/`。每次运行使用新的日期/run 目录，已有输出目录会导致失败，禁止覆盖已归档结果。

## Manifest

当前正式 `dev` 清单为 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/evals/benchmark_manifest.json`。它锁定题集、split、隔离快照、18 个 `dev` 题 ID、比较范围和策略配置。更换数据、快照或策略时，复制并审阅清单；不得把 API key、Authorization Header 或环境变量写入清单。

`keyword` 不调用外部服务。`vector` 和 `hybrid` 从 `backend/.env` 读取 Embedding 配置；每题发送一次 query embedding 请求，不自动重试。正式 `dev` 为 18 题，因此每次 vector 或 hybrid 运行最多发起 18 次 Embedding 请求。Benchmark 不调用 LLM。请求用量/费用由 Provider 计费侧核实，报告不推算未知费用。

## 运行命令

命令从 `/home/abeltomato/workspace/projects/Tomato-Agent/backend` 执行。输出目录必须是尚不存在的新目录：

```bash
.venv/bin/python -m app.knowledge.benchmark run \
  --manifest /home/abeltomato/workspace/projects/Tomato-Agent/backend/evals/benchmark_manifest.json \
  --strategy keyword-v1 --split dev \
  --output /home/abeltomato/workspace/projects/Tomato-Agent/backend/data/rag/reports/YYYY-MM-DD/public-blog/dev/benchmark-v1/keyword-v1
```

Vector/hybrid 使用相同 manifest 结构，但將 `strategy.name`、`strategy.mode`、Embedding 模型/维度和阈值改为待测配置，保存为独立 manifest，再传入该文件运行。不得让 `--strategy` 或 `--split` 与 manifest 不一致。

比较两个已生成的 run 报告：

```bash
.venv/bin/python -m app.knowledge.benchmark compare \
  --baseline /home/abeltomato/workspace/projects/Tomato-Agent/backend/data/rag/reports/YYYY-MM-DD/public-blog/dev/benchmark-v1/keyword-v1/report.json \
  --candidate /home/abeltomato/workspace/projects/Tomato-Agent/backend/data/rag/reports/YYYY-MM-DD/public-blog/dev/benchmark-v1/vector-v1/report.json \
  --output /home/abeltomato/workspace/projects/Tomato-Agent/backend/data/rag/reports/YYYY-MM-DD/public-blog/dev/benchmark-v1/comparison-keyword-vector
```

Run 产生 `report.json`、`report.md`；compare 产生 `comparison.json`、`comparison.md`。报告保存逐题结果、类别汇总、Evidence Recall@1/3/5、MRR@5、Hit@5、空结果率、错误率、延迟、数据/语料/索引/源码指纹和策略配置。延迟受机器、并发和 Provider 状态影响，不同计时条件不可直接比较。

## 状态解释与失败处理

- `complete`：选定 split 的所有题目都有完成结果；不代表检索质量或答案语义通过。
- `incomplete`：至少一题执行失败；失败题以 `error` 状态保留，执行错误不算空结果或正确拒答，CLI 返回非零状态。
- `incomparable`：题集、语料、指标版本或未声明的索引差异不允许直接比较；不得宣称提升。
- 比较结果是描述性差异，不是统计显著性检验或发布准入结论。

报告目录不可覆盖。失败或重新运行时创建新目录并保留旧工件。先检查 manifest、配置和数据库指纹；不要把日志中的异常等同于零结果。

## 验证

固定夹具使用标准库 SQLite 和 Fake Embedding Provider，不访问外部服务：

```bash
.venv/bin/python -m pytest \
  tests/test_knowledge_benchmark_models.py \
  tests/test_knowledge_benchmark_snapshot.py \
  tests/test_knowledge_benchmark.py \
  tests/test_knowledge_benchmark_compare.py \
  tests/test_knowledge_benchmark_evaluation.py \
  tests/test_knowledge_dataset.py \
  tests/test_retrieval.py -q
```

真实基线必须先获得快照与外部调用范围批准；只运行 `dev`，不触碰业务数据库，不查看 `test`。人工答案正确性、完整性、groundedness 和 citation fidelity 需单独人工评估，不能由检索指标替代。