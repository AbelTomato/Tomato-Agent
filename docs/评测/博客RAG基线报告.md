# 博客 RAG 基线报告

## 1. 报告状态

- **报告日期**：2026-09-28（含 Benchmark 工程基线及 2026-09-22/23 历史基线）
- **状态**：已完成真实博客正式题集 `dev` 的三模式离线检索基线、同策略 keyword 重复检查、历史 18 题真实 LLM 人工语义验收、18 题 hybrid Pipeline 重测、2026-09-24 Observe 工件复验，以及 2026-09-28 获准的三策略 hybrid 消融；最新任务 3 消融工件已归档，但 `014` 最终双证据和无答案语义拒答门禁仍未通过。**RAG 产品质量验收仍未通过**。
- **范围限制**：本报告使用 66 篇真实博客的隔离快照和 36 题正式题集中的 18 题 `dev`。`test` 集未查看；2026-09-23 Pipeline 重测未调用 LLM 做 query splitting、答案生成或语义 judging；2026-09-24 Observe runner 使用 `keyword` 检索，18 题均无候选，实际 Provider 请求/尝试/响应为 `0/0/0`。历史 LLM 验收不代表后续 Pipeline 的生成质量；Observe 工件完整不代表问答验收完成，结果不代表最终博客问答质量，也不构成新策略收益结论。

## 2. 可复现实验入口

Benchmark run/compare 的固定夹具验证、manifest 和报告字段说明见 [`Benchmark使用说明.md`](Benchmark使用说明.md)。本轮真实 Benchmark 工件位于 `backend/data/rag/reports/2026-09-24/public-blog/dev/benchmark-v1/`；该目录包含三模式逐题报告、keyword 重复 run、配对比较和 `provenance.json`。只运行 dev，未访问 test；vector/hybrid 使用现有只读索引，每题一次 query Embedding，无重试；未调用 LLM。

### 2.1 2026-09-24 Benchmark 三模式 `dev` 基线

执行题集为正式问题集的 `dev` 18 题（可回答 15、无答案 3），快照为 `public-blog-2026-09-23-v1.db`。题集 SHA-256 为 `ee24b12743fa41dad6e4f7c2f55632be5063292817362a4a31d206459b812992`；SQLite 文件 SHA-256 为 `20e2ec267c4bd7f9b78ba5b9f8821514fc029ea8fb420ba0065e2a7cbf3504d4`，知识表指纹为 `3aa1e81f058aa27e71a423478a43843788ae83c1b748aed13f96a99393dd2e9c`；快照含 66 documents、886 chunks、886 embeddings，`integrity_check=ok`。实验全程只读快照，未查看 test split、未访问业务数据库。运行和比较工件及 SHA-256 清单见同目录 `provenance.json`。

| 模式 | 配置 | Recall@5 | MRR@5 | Hit@5 | 可回答空结果 | 无答案空结果 | 无答案非空结果 | 执行错误 | 延迟 p50/p95 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| keyword | BM25，top-k 5 | 0/15 | 0/15 | 0/15 | 15/15 | 3/3 | 0/3 | 0/18 | 146/220 ms |
| vector | `qwen3.7-text-embedding-flash`，1024 维，余弦阈值 0.5 | 14/15（0.9333） | 0.8222 | 14/15（0.9333） | 1/15 | 3/3 | 0/3 | 0/18 | 533/1007 ms |
| hybrid | 同一 Embedding，RRF 前余弦阈值 0.2 | 14.5/15（0.9667） | 0.8889 | 15/15 | 0/15 | 0/3 | 3/3 | 0/18 | 684/966 ms |

keyword 同策略重复 run 比较状态为 `unchanged`；除延迟外的汇总与分类质量指标一致。对比器结果：keyword→vector 为描述性 `improved`，keyword→hybrid 和 vector→hybrid 为 `mixed`。小样本结果不代表统计显著性。vector 和 hybrid 分别对 18 题发出一次 Embedding 请求，总计 36 次；无自动重试、无 LLM 请求。Provider usage/billing 不可由客户端报告核实，费用保持未知。

质量解释：vector 在本次阈值下无答案检索为空 3/3；hybrid 的证据召回较高，但三个无答案题均检索非空（3/3）。检索空结果只是拒答代理指标，非空也不能单独判为语义错误回答；仍需独立答案生成和人工拒答/引用支撑复核。已知 `blog-formal-014` 最终证据覆盖风险不因整体 Recall 提升而关闭。本结果**不是 RAG 答案质量通过或上线许可**。

### 2.2 2026-09-28 任务 1：冻结验收契约与对照基线

本节是任务 1 的当前验收矩阵，不改写 2026-09-23/24 的历史实验事实，也不宣称 RAG 产品质量通过。

| 验收项 | 冻结事实与分母 | 可追溯证据 | 结论 |
|---|---|---|---|
| A1 输入与快照 | 原始题集文件 36 题，SHA-256 `ee24b12743fa41dad6e4f7c2f55632be5063292817362a4a31d206459b812992`；选定 `dev` 规范化题集 18 题，指纹 `0d320487ad2f76269ba225bb43f2f3236f6b8759606a6a0478db11baa2dbf4f3`；快照文件 SHA-256 `20e2ec267c4bd7f9b78ba5b9f8821514fc029ea8fb420ba0065e2a7cbf3504d4`；SQLite `integrity_check=ok`，66 documents、886 chunks、886 embeddings | `/home/abeltomato/workspace/projects/Tomato-Agent/backend/evals/benchmark_manifest.json`、2026-09-24 Benchmark `report.json`、只读 SQLite 核验 | 通过 |
| A2 题集与标注范围 | manifest 与规范化 `dev` 题集 ID 完全一致：`blog-formal-001`–`blog-formal-018`；可回答 15 题、无答案 3 题（`013/017/018`）；可回答题共 17 条标注 evidence span，`014` 占 2 条、`015` 占 2 条，其余 13 题各 1 条；所有 `document_version`、物理起止行均通过范围校验 | `load_questions(..., split="dev")`、`benchmark_snapshot.py` 的 `EvidenceSpan` 校验、manifest `question_ids/question_splits/question_groups` | 通过 |
| A3 对照策略与有效配置 | 三个已归档对照 run 均为 `status=complete`、相同 `metric_version=benchmark-metrics/v1`、相同 corpus/index/source 指纹和 `dev` 题集指纹：`keyword-v1`（BM25，candidate 30/final 5，阈值 0.0）、`vector-v1`（cosine，candidate 30/final 5，阈值 0.5，`qwen3.7-text-embedding-flash`/1024 维）、`hybrid-v1`（RRF，candidate 30/final 5，余弦阈值 0.2，同一 Embedding） | `/home/abeltomato/workspace/projects/Tomato-Agent/backend/evals/benchmark_manifest.json`、`backend/data/rag/reports/2026-09-24/public-blog/dev/benchmark-v1/*/report.json`；三 run `source_sha256` 均为 `796a8d174f63e9ef9a3ed3da33022e491d2eab36a572a6cc6285168c6b02f22b` | 通过；仅作描述性对照 |
| A4 逐题字段与语义边界 | `evaluation.py` 的逐题结果分别记录 candidate/final 证据、证据/来源覆盖、Judge 状态与 reason、LLM 是否调用、阶段/端到端耗时、错误；离线检索报告的 `answer_quality_evaluated` 固定为 `false`，无答案非空不能计为正确拒答，执行错误不计为空结果或正确拒答 | `EvaluationQuestion`/`QuestionResult`、`evaluate_question_results`、`test_knowledge_evaluation.py` 现有断言；`014` 仍单列 candidate/final 覆盖，三道无答案仍单列非空风险 | 通过；不替代任务 4/5 人工答案审计 |

题集当前 schema 没有独立的 `annotation_version` 字段；本轮不擅自新增字段。标注版本由每条 `relevant_spans` 的 `document_version` 与物理行范围表达，已纳入 A2 校验。机器 Judge 的 `supported`、引用白名单合法性和报告 `complete` 均不代表答案语义通过；`014` 双证据最终覆盖与三道无答案题的安全拒答仍是后续任务门禁。

评测 CLI 固定为：

```bash
cd /home/abeltomato/workspace/projects/Tomato-Agent/backend
.venv/bin/python -m app.knowledge.evaluation \
  --dataset /absolute/path/to/questions.jsonl \
  --split dev \
  --mode keyword \
  --output /absolute/path/to/report.json \
  --database /absolute/path/to/knowledge.db
```

`--mode` 支持 `keyword`、`vector` 和 `hybrid`。向量类模式必须显式提供 `--embedding-model` 和 `--embedding-dimensions`，并使用已写入同一知识库的兼容索引；不满足条件时命令失败，不静默降级到关键词检索。向量/混合模式可通过 `--min-vector-similarity` 设置原始余弦相似度最低门槛，合法范围为 `0..1`；`hybrid` 在 RRF 前执行该过滤，不能用 RRF 分数替代余弦相似度。

同一语料快照和同一问题划分下，应分别运行三种模式。离线检索延迟由报告单独记录；模型生成耗时、模型 usage 和费用不由该 CLI 伪造或推断。CLI 的答案质量字段固定标记为未评估，需要人工检查引用是否支持主要结论、是否遗漏限定以及无答案问题是否正确拒答。

本次 vector/hybrid smoke 使用 `--min-vector-similarity 0.5`；keyword 模式不使用该阈值。报告中的阈值仅表示拒答门禁配置，不表示答案质量分数。

## 3. 指标定义

- `Recall@5`：按标注证据段统计。只有检索片段与标注段属于相同文档版本，且完整覆盖标注起止行时，才算命中。
- `MRR@5`：第一个覆盖任一标注证据段的结果排名倒数；无结果或未命中为 0。
- 无答案问题不进入召回和 MRR 分母，单独统计正确拒答与错误作答。
- 标注证据段覆盖率和首个命中排名由自动指标计算，不能替代人工语义支撑评估。

## 4. 当前自动验证结果

数据集文件为 `backend/evals/blog_questions_smoke.jsonl`，划分为 `dev`，问题数为 5（可回答 4、无答案 1），数据集 SHA-256 为 `eae295da84310816a789144bbfdfa8e2e1cf088bb0817c597cdb92c32dd1dc6b`。三种模式使用同一数据库和同一 `top_k=5`：

| 模式 | 最低向量相似度 | Recall@5 | MRR@5 | 无答案正确拒答 |
|---|---:|---:|---:|---:|
| keyword | 不适用 | 0 | 0 | 1/1 |
| vector | 0.5 | 0.875 | 0.6875 | 1/1 |
| hybrid | 0.5（RRF 前过滤） | 0.875 | 0.6875 | 1/1 |

结果表明：在这个 5 题 smoke 集上，默认 `0.5` 门槛保留了 vector/hybrid 的可回答证据召回，并拒绝了无答案问题；keyword 行为不受该阈值影响。答案质量字段仍为 `not evaluated by offline retrieval CLI`，以上指标不能替代人工语义支撑评估，也不能外推为完整博客问答准确率。

工程边界测试还验证了：非法阈值（非有限值或超出 `0..1`）被拒绝；低于阈值的 vector 结果不会进入引用；hybrid 不会让关键词结果绕过失败的向量置信度门禁；通过门槛时原有引用快照仍被保留。

### 4.1 正式题集 `dev` 三模式基线

正式题集为 `backend/evals/blog_questions.jsonl`，文件 SHA-256 为 `ee24b12743fa41dad6e4f7c2f55632be5063292817362a4a31d206459b812992`；本次 `dev` 为 18 题（15 个可回答、3 个无答案）。最初 v3 快照将 YAML front matter 作为 927 个片段中的一部分入库；该历史基线用于定位问题，不能与下列 v4 直接作策略优劣比较。

当前 v4 隔离快照为 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/data/rag/databases/public-blog-2026-09-23-v1.db`（SHA-256：`20e2ec267c4bd7f9b78ba5b9f8821514fc029ea8fb420ba0065e2a7cbf3504d4`），包含 66 个文档、886 个正文片段，66/66 文档索引状态为 `ready`。YAML front matter 已排除，正文行号保持原文物理行号；向量配置为 `qwen3.7-text-embedding-flash`、1024 维；vector/hybrid 使用 `top_k=5` 和最低余弦相似度 `0.5`。早期临时快照路径只作为历史运行记录，不作为后续实验路径。

| 模式 | 可回答空结果 | Recall@5 | MRR@5 | Hit@5 | 无答案空结果 | 执行错误 |
|---|---:|---:|---:|---:|---:|---:|
| keyword | 15/15 | 0 | 0 | 0 | 3/3 | 0 |
| vector | 1/15 | 0.9333 | 0.8222 | 0.9333 | 3/3 | 0 |
| hybrid | 1/15 | 0.9333 | 0.8222 | 0.9333 | 3/3 | 0 |

vector/hybrid 结果相同，不表示两种策略已经优劣等价；本次样本和排序配置下尚未观察到差异。v4 中 `blog-formal-014` 不再返回 front matter，而是在 `0.5` 门槛下成为唯一可回答空结果。仅在 `dev` 的诊断显示：门槛降为 `0.0`、`top_k=20` 时其两条正文证据排名为第 1 和第 7，Evidence Recall 为 1.0；但三个无答案题均变为非空结果。因此不能将调低单一门槛或增加最终返回数表述为已解决。

### 4.2 正式题集 `dev` LLM 人工验收

使用与上述 v4 hybrid 基线一致的 `top_k=5`、相似度门槛和隔离快照，模型为 `gpt-5.6-terra`。结果文件仅保存在临时目录 `/tmp/tomato-rag-admission-reports-20260922/formal-dev-v4/llm.json`，SHA-256 为 `65de27668b61c51f0f0de84b51999f12941dcd84589073183e2959a18fd1a941`。

| 状态 | 数量 | 解释 |
|---|---:|---|
| 返回 `supported` | 14 | 通过结构和请求内 `R1…Rn` 引用标签校验；人工核对其主要结论由返回引用支撑 |
| 正确 `no_results` | 3/3 | 三个无答案题均未生成答案，拒答代理通过 |
| 可回答误拒答 | 1 | `blog-formal-014` 在默认门槛下没有检索结果，未调用模型 |
| 执行失败 | 0 | v3 的三例引用越界已不再出现；服务端仍保留 citation 白名单校验 |

“人工语义完全通过”是本次人工核对的保守记录，不是自动化质量指标；没有将其升级为总体准确率或上线许可。

### 4.3 代表性失败样例与修复状态

| 问题 | 现象 | 归因 | 下一步 |
|---|---|---|---|
| `blog-formal-001` | v3 中引用越界；v4 已返回 Alembic 18–30 行的有效引用 | 长哈希 citation ID 容易被模型改写或臆造 | 保留白名单；请求内短标签 `R1…Rn` 映射回真实 citation，v4 已关闭该失败 |
| `blog-formal-003` | v3 中引用越界；v4 已返回 FastAPI session 生命周期正文引用 | 同上，且同一主题内出现重复 | 与 001 一同作为引用协议回归样例，v4 已关闭 |
| `blog-formal-007` | v3 中引用越界；v4 已返回 `Depends(get_db)` 正文引用 | 同上 | 与 001 一同作为引用协议回归样例，v4 已关闭 |
| `blog-formal-014` | v3 以 front matter 伪支撑；v4 默认门槛下变为 `no_results`；本次 Pipeline 候选覆盖 2/2、最终只覆盖 1/2 | front matter 已排除；最终证据选择仍遗漏一条跨文章证据 | 继续只在 dev 检查候选覆盖与最终选择；不得将候选命中当成最终支撑或语义通过 |
| 无答案 `013/017/018` | 本次 candidate/final 均非空且 Coverage Judge 为 `supported`；本次未调用 LLM | 覆盖 Judge 的结构判定不能替代无答案语义验证 | 不计为正确拒答；需要另行批准并执行语义/拒答评估前，不得宣称安全性通过 |


### 4.4 2026-09-23 当前 Pipeline hybrid 重测

本次运行读取同一只读快照和同一 `dev` split，实际执行 `SafeQueryPlanner → RepositoryCandidateRetriever → NoopReranker → CoverageAwareEvidenceSelector → CoverageAnswerabilityJudge`。参数固定为 candidate limit `30`、candidate 最低向量相似度 `0.2`、final limit `5`；没有 query splitting、reranking、答案生成或 LLM judge。18 题逐题结果、各阶段延迟和 provenance 保存在本地忽略目录 `backend/data/rag/reports/2026-09-23/public-blog/dev/pipeline-v1/`。

| 范围 | 证据 Recall | MRR | Hit | 可回答空结果 | 非空无答案候选/最终结果 | Judge 状态 |
|---|---:|---:|---:|---:|---:|---|
| candidate pool（limit 30） | 1.0000 | 0.8889 | 1.0000 | — | 3/3 候选非空 | `supported` 18、`insufficient` 0、`no_results` 0、执行错误 0 |
| final evidence（limit 5） | 0.9667 | 0.8889 | 1.0000 | 0/15 | 3/3 最终结果非空 | 同上 |

本次执行错误 `0/18`，总阶段延迟 mean/p50/p95/max 为 `1122.64/1032.85/1917.74/1917.74 ms`；其中 candidate 阶段 mean/p95 为 `1122.21/1917.23 ms`。其余阶段延迟和逐题明细以 JSON 工件为准。本地快照仍为 66 documents、886 chunks、886 embeddings，SQLite integrity check 为 `ok`；评测以只读模式打开该数据库。

`blog-formal-014` 的两条必需标注证据均出现在候选池（2/2），最终五条证据只覆盖 1/2；来源覆盖为 1/2。三个无答案题最终均有非空结果，Coverage Judge 都给出 `supported`。这是基于检索面覆盖率的 Judge 状态统计，**不等同于答案生成，也不构成语义正确性验证或拒答质量评估**；本次没有调用 LLM，`answer_quality` 明确为未评估，`correct_refusal_count` 与 `incorrect_refusal_count` 均为 `0` 且 refusal quality 未评估。因此该重测未解除 `blog-formal-014` 最终证据缺失，也未证明无答案安全性，**RAG 质量验收仍未通过**。本次指标仅记录当前流水线行为，不作策略收益或语义质量结论。

正式逐题报告为 `backend/data/rag/reports/2026-09-23/public-blog/dev/pipeline-v1/hybrid.json`，SHA-256 `092490970f52208768442d38794b549ecf4919a77f9669ec3ebe4319150ccec2`；同目录 `summary.json`（SHA-256 `83bf78f01d3530936b88e2caf97ec3c06f442d40f65b37a6b329e6443c94f8ea`）和 `provenance.json`（SHA-256 `e918fb248f22304e1af3fd085089115da6ddaf26051586fadca2148a37c79256`）记录汇总与数据/代码指纹。该目录与旧 `admission-v1` 分离；本次未重建索引、未查看 `test`，也未修改旧归档工件。
这些失败样例来自真实博客 `dev`，不是固定夹具或合成题；历史 LLM 输出保存在旧归档，本次逐题 Pipeline 结果保存在新报告中。执行失败不计为正确拒答，Coverage Judge 的 `supported` 状态也不代表语义验证；本次未把 `blog-formal-014` 的候选命中误记为最终覆盖。

### 4.5 2026-09-24 Observe 任务 6 复验状态

离线 `rag-observe-trace/v1` runner 与集成工件完整性检查已通过固定知识库快照和 Fake LLM 测试（任务 6 指定组合测试 198/198）；该测试验证实现和工件契约，不代表真实答案生成。随后按批准范围显式运行 `--real-llm`，候选 run `24f1c9e3-52fb-4fa8-916f-2709a729dd30` 已生成；因为当前 runner 固定使用 `keyword` 检索，而 18 道正式 `dev` 题均无检索候选，18 次 Answerer 调用全部按 `answerability_no_results` 跳过。实际 provider 请求/尝试/响应为 **0/0/0**，执行错误为 0；因此本次没有真实 LLM 答案、引用白名单校验不适用，人工复核为 **0/18**，不得把本次表述为 18 次真实 LLM 问答或质量验收。汇总 `complete` 只表示 run 生命周期和工件完整，不代表评测目标达成。报告位于 `backend/data/rag/reports/2026-09-24/public-blog/dev/observe-v1/24f1c9e3-52fb-4fa8-916f-2709a729dd30/`，Trace schema 为 `rag-observe-trace/v1`；18/18 逐题文件、126 条事件及 22 个 manifest 工件均通过事件链/文件大小/SHA-256 校验。题集 SHA-256 前后均为 `ee24b12743fa41dad6e4f7c2f55632be5063292817362a4a31d206459b812992`；只读快照 SHA-256 前后均为 `20e2ec267c4bd7f9b78ba5b9f8821514fc029ea8fb420ba0065e2a7cbf3504d4`，SQLite integrity 为 `ok`。provider 不暴露 usage/billing，尽管 Observe 记录没有请求，实际费用仍无法从客户端核实；批准的 `$5` 预算不可本地强制或保证。Benchmark 准入结论不变。下一次真实 LLM 评估需先解决该 runner 的检索模式限制/证据候选缺失，使用新 run ID 并重新确认批准范围；不得重用本次目录或把本次完整状态解释为语义验收。

### 4.6 2026-09-28 任务 3：获准 hybrid 消融与离线准入

本次只运行版本化配置 `backend/evals/blog_retrieval_ablation_task3.json` 中显式批准的 `baseline`、`wide-candidate`、`coverage-aware` 三项策略；三者均为 `mode=hybrid`、query planning `disabled`、rerank `noop`，只使用 `dev` 18 题和同一只读快照。Embedding 来自 `backend/.env` 配置的 `qwen3.7-text-embedding-flash`（1024 维）；没有 LLM、reranker Provider 或业务数据库调用，也未读取 `test`。运行前后题集 SHA-256 均为 `ee24b12743fa41dad6e4f7c2f55632be5063292817362a4a31d206459b812992`，快照 SHA-256 均为 `20e2ec267c4bd7f9b78ba5b9f8821514fc029ea8fb420ba0065e2a7cbf3504d4`，SQLite `integrity_check=ok`。

| 策略 | candidate/final 配置 | Recall@5 | MRR@5 | Hit@5 | `014` candidate/final | `014` 来源覆盖 | 可回答空结果 | 无答案 candidate/final 非空 | 执行错误 | p50/p95 延迟 |
|---|---|---:|---:|---:|---|---|---:|---|---:|---:|
| baseline | 5 / 5，候选余弦阈值 0.5 | 0.9333 | 0.8222 | 0.9333 | 0/2 / 0/2 | 0/2 | 1/15 | 0/3 / 0/3 | 0/18 | 552/678 ms |
| wide-candidate | 30 / 5，候选余弦阈值 0.2 | 0.9667 | 0.8889 | 1.0000 | 2/2 / 1/2（candidate ranks 7、1；final rank 1） | 1/2 | 0/15 | 3/3 / 3/3 | 0/18 | 554/693 ms |
| coverage-aware | 30 / 5，候选余弦阈值 0.2 | 0.9667 | 0.8889 | 1.0000 | 2/2 / 1/2（candidate ranks 7、1；final rank 1） | 1/2 | 0/15 | 3/3 / 3/3 | 0/18 | 534/620 ms |

每个策略均为 `status=complete`，每个策略发出 18 次 Embedding 请求，失败 0、重试 0、LLM 请求 0；Provider usage/billing 未返回，费用保持未知。宽候选两项仅相对 baseline 做描述性配对比较，未声明统计显著性。`wide-candidate` 与 `coverage-aware` 虽然消除了 15 道可回答题的空结果，但 `blog-formal-014` final 仍只覆盖一条标注证据和一个来源；三道无答案题的 candidate/final 均非空，Coverage Judge 的 `supported`/`full_query_coverage` 只是结构状态，不能被写成正确拒答或语义安全结论。Baseline 仍为保留策略。

逐题报告、汇总、请求统计、代码/配置指纹和 provenance 位于 `backend/data/rag/reports/2026-09-28/public-blog/dev/task3-ablation-20260928/`：根目录 `comparison.json`，以及各策略的 `report.json`、`question_results.json`、`summary.json`、`provenance.json`。目录权限为 `700`，文件权限为 `600`。任务 3 离线准入**未通过**：没有策略满足 `014` final 2/2 证据与来源覆盖，宽候选策略还引入无答案非空风险；因此不进入任务 4，不执行答案审计或 `test` 评估。

## 5. 准入结论与下一步
当前结论：**已达到 Benchmark 工程实现的启动条件，但 RAG 产品质量验收仍未通过。** 现有题集、隔离快照、指标、逐题结果和失败记录足以支持建立可追溯的 Benchmark 工具；这不代表 `blog-formal-014` 的最终证据缺失或无答案语义风险已经关闭，也不构成上线许可。引用越界和 front matter 伪支撑已关闭；`blog-formal-014` 最终证据仅覆盖 1/2，以及三个无答案题缺少语义验证，继续作为固定风险样例跟踪。

1. 在 `dev` 上先写候选池/拒答策略的离线测试，明确候选深度、文档或证据多样性选择、最终返回上限和拒答门禁；不得引入未经设计的 reranker。
2. 使用相同题集和 v4 语料快照，仅比较显式声明的策略；同时报告 `014` 两条证据、三个无答案题、可回答空结果和延迟，不能只看总体 Recall。
3. 候选策略通过 dev 离线验收后，复跑 18 题真实 LLM 验收并人工核对引用支撑；未关闭可回答假阴性前，不查看 `test`。
4. 记录索引耗时、Embedding 请求成本/失败重试统计和完整实验指纹；当前报告不推断未采集的费用。
5. 按 Benchmark 计划从任务 1 的实验契约开始；在此之前及实施过程中都不创建虚构 candidate，不把工程准入或单次小样本结果宣称为策略收益、答案质量通过或上线许可。