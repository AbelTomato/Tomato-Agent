# 博客 RAG 基线报告

## 1. 报告状态

- **报告日期**：2026-09-23（含 2026-09-22 历史基线）
- **状态**：已完成真实博客正式题集 `dev` 的三模式离线检索基线、历史 18 题真实 LLM 人工语义验收，以及当前 Pipeline 的 18 题 hybrid 重测；**RAG 尚未达到 Benchmark 实施准入门槛**。
- **范围限制**：本报告使用 66 篇真实博客的隔离快照、36 题正式题集中的 18 题 `dev`，以及已批准的 Embedding/LLM 配置。`test` 集未查看；本次 Pipeline 重测仅调用 Embedding API，不调用 LLM 做 query splitting、答案生成或语义 judging。历史 LLM 验收不代表本次 Pipeline 重测的生成质量；结果不代表最终博客问答质量，也不构成新策略收益结论。

## 2. 可复现实验入口

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

当前 v4 隔离快照为 `/tmp/tomato-public-blog-rag-admission-20260922-v4.db`，包含 66 个文档、886 个正文片段，66/66 文档索引状态为 `ready`。YAML front matter 已排除，正文行号保持原文物理行号；向量配置为 `qwen3.7-text-embedding-flash`、1024 维；vector/hybrid 使用 `top_k=5` 和最低余弦相似度 `0.5`。

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

`blog-formal-014` 的两条必需标注证据均出现在候选池（2/2），最终五条证据只覆盖 1/2；来源覆盖为 1/2。三个无答案题最终均有非空结果，Coverage Judge 都给出 `supported`。这是基于检索面覆盖率的 Judge 状态统计，**不等同于答案生成，也不构成语义正确性验证或拒答质量评估**；本次没有调用 LLM，`answer_quality` 明确为未评估，`correct_refusal_count` 与 `incorrect_refusal_count` 均为 `0` 且 refusal quality 未评估。因此该重测未解除 `blog-formal-014` 最终证据缺失，也未证明无答案安全性，Benchmark 准入仍不通过。本次指标仅记录当前流水线行为，不作策略收益或语义质量结论。

正式逐题报告为 `backend/data/rag/reports/2026-09-23/public-blog/dev/pipeline-v1/hybrid.json`，SHA-256 `092490970f52208768442d38794b549ecf4919a77f9669ec3ebe4319150ccec2`；同目录 `summary.json`（SHA-256 `83bf78f01d3530936b88e2caf97ec3c06f442d40f65b37a6b329e6443c94f8ea`）和 `provenance.json`（SHA-256 `e918fb248f22304e1af3fd085089115da6ddaf26051586fadca2148a37c79256`）记录汇总与数据/代码指纹。该目录与旧 `admission-v1` 分离；本次未重建索引、未查看 `test`，也未修改旧归档工件。
这些失败样例来自真实博客 `dev`，不是固定夹具或合成题；历史 LLM 输出保存在旧归档，本次逐题 Pipeline 结果保存在新报告中。执行失败不计为正确拒答，Coverage Judge 的 `supported` 状态也不代表语义验证；本次未把 `blog-formal-014` 的候选命中误记为最终覆盖。
## 5. 准入结论与下一步
当前结论：**未达到 `/home/abeltomato/workspace/projects/Tomato-Agent/docs/实施计划/RAG完成后Benchmark实施计划.md` 的实施准入门槛，暂不进入 Benchmark 任务 1。** 引用越界和 front matter 伪支撑已关闭；当前未满足项包括 `blog-formal-014` 最终证据仅覆盖 1/2，以及三个无答案题均得到 Coverage Judge `supported` 且本次没有语义验证，故仍不符合“无阻断性功能问题”。

1. 在 `dev` 上先写候选池/拒答策略的离线测试，明确候选深度、文档或证据多样性选择、最终返回上限和拒答门禁；不得引入未经设计的 reranker。
2. 使用相同题集和 v4 语料快照，仅比较显式声明的策略；同时报告 `014` 两条证据、三个无答案题、可回答空结果和延迟，不能只看总体 Recall。
3. 候选策略通过 dev 离线验收后，复跑 18 题真实 LLM 验收并人工核对引用支撑；未关闭可回答假阴性前，不查看 `test`。
4. 记录索引耗时、Embedding 请求成本/失败重试统计和完整实验指纹；当前报告不推断未采集的费用。
5. 准入通过后，再按 Benchmark 计划从任务 1 的实验契约开始；在此之前不创建虚构 candidate，也不宣称策略收益。