# 个人博客 RAG 与 Agent 开发实施计划

> 执行说明：使用 `executing-plans` 按任务顺序执行；每次只推进一个可验证切片。本文是实施计划，不代表功能已经实现。所有任务初始均未完成，不自动提交 Git。

**目标：** 面向 AI 应用／Agent 开发实习，将 Tomato Agent 建设为具备可追溯知识问答、检索评测和人工确认写作流程的个人技术知识助手。

**架构：** 复用 FastAPI、现有 Agent Runtime、工具注册和 SQLite 持久化。第一版使用确定性检索问答流程，第二版将同一检索服务接入 Agent 工具，通过服务端状态机控制提纲确认和草稿保存；前端复用 React。

**技术栈：** Python >=3.11、FastAPI、Pydantic、aiosqlite、httpx、tiktoken、pytest、pytest-asyncio；React、TypeScript、Vite。新增模块优先使用已有依赖和标准库，不预先引入 Agent 框架或独立向量数据库。

**需求依据：** 本轮已确认的“补齐多轮会话 → 博客入库与带引用 RAG → 检索评测 → 技术写作 Agent”方向；现状参考 `/home/abeltomato/workspace/projects/Tomato-Agent/README.md` 和 `/home/abeltomato/workspace/projects/Tomato-Agent/docs/系统设计.md`。这两份文档部分描述落后于代码，不能作为已实现能力的证明。

## 1. 范围、约束与执行条件

- 版本 A：单用户本地博客知识助手，支持导入、增量更新、带来源问答和连续追问。
- 版本 B：基于旧博客生成技术提纲，经用户确认后生成草稿，预览并再次确认后保存。
- 本期不包含网页爬虫、PDF/OCR、联网补资料、长期记忆自动写入、多 Agent、GraphRAG、微调和桌面自动操作。
- 桌面悬浮形象和 MCP 仅列入后续扩展，不作为 A/B 的完成条件。
- 保留现有会话和工具接口的兼容性；新增返回字段使用默认值，旧客户端仍能读取 `answer`。
- 仅绑定本机使用，不将无鉴权接口直接发布公网；公网部署另立鉴权与数据隔离任务。
- 博客正文、检索片段和模型输出均视为不可信数据；提示词标签不能代替工具白名单和服务端确认校验。
- 凭据通过环境变量配置，不写进代码、评测报告或运行日志。自动测试使用临时数据库、固定语料与 Fake Provider。
- 执行核心行为变更时，先运行相关基线测试，再补失败测试、实现、验证；不重复运行代码未变化且已通过的检查。
- 本文所有绝对路径以当前工作区为准；移动项目时统一替换前缀。

### 执行前输入

| 输入 | 当前情况 | 处理方式 |
|---|---|---|
| 博客原始 Markdown 所在目录 | 尚未提供 | 先开发固定测试语料；真实导入前获取路径，不扫描任意个人目录 |
| 博客 URL 和标题映射 | 尚未提供 | 使用显式 JSON 清单；缺少 URL 的文章通过内部文档接口查看 |
| Embedding 服务、模型、维度与费用预算 | 尚未确定 | 先实现协议与 Fake；真实实验前确认支持 `/embeddings` 的服务及费用 |
| 真实聊天模型配置 | 有适配器，尚未验证服务可用性 | 使用实际环境变量配置；不把代码中的默认模型名当作可用性保证 |
| 博客内容是否可发送给外部模型 | 尚未确认 | 真实调用前确认；本地保存索引不代表数据完全不出设备 |

这些输入只阻塞真实数据和外部服务验收，不阻塞离线功能与测试开发。不得用模拟服务的结果冒充真实模型效果。

## 2. 现状与优先修复项

1. `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/main.py` 每条消息新建 Run；`/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/agent/runtime.py` 仅回放当前 Run 的事件，尚未将历史轮次提供给新 Run。
2. `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/sessions/repository.py` 已有 `list_session_events`，应优先复用并明确跨 Run 排序和过滤规则。
3. `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/tools/search.py` 默认返回 Mock；不能用于真实检索验收。
4. `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/agent/context.py` 是确定性文本压缩；目前没有完整长期记忆召回与更新流程。
5. `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/llm/compatible_client.py` 只取首个工具调用；本期保持串行工具策略，但必须显式拒绝多个调用，避免静默丢弃。
6. Runtime 有暂停和检查点基础，HTTP 尚无完整恢复入口；已有检查点不等于工具副作用具有恰好一次执行保证。
7. `/home/abeltomato/workspace/projects/Tomato-Agent/frontend/src/main.tsx` 是基础聊天入口，缺少引用、历史恢复和任务确认界面。

## 3. 里程碑与依赖

| 里程碑 | 任务 | 独立交付物 | 估计投入 |
|---|---|---|---|
| M0：可信会话与验收语料 | 1–2 | 可连续追问的 Runtime；首批问题及证据标注 | 3–5 个工作日 |
| M1：版本 A 功能闭环 | 3–6 | 可导入、检索、问答、追问和查看来源 | 7–10 个工作日 |
| M2：版本 A 评测 | 7 | 可复现的基线对比与失败分析 | 3–5 个工作日 |
| M3：版本 B | 8–10 | 检索→提纲→确认→草稿→确认保存 | 7–10 个工作日 |

估算不是承诺；数据清洗、服务可用性和个人投入时间会影响进度。任务依赖为 `1 → 5`、`2 → 3 → 4 → 5 → 6`、`4/5 → 7`、`5/7 → 8 → 9 → 10`。任务 1 与 2 可独立推进。完成 M2 后即可整理第一版求职材料。

## 4. 文件职责与接口约定

### 新增后端模块

以下文件均在 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/knowledge/` 下创建，并添加空 `__init__.py`：

| 完整路径 | 职责 |
|---|---|
| `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/knowledge/models.py` | 文档、片段、命中、引用、回答模型 |
| `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/knowledge/ingestion.py` | 读取清单、清洗 Markdown、分块、内容哈希 |
| `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/knowledge/repository.py` | 文档、片段、向量及索引版本的事务存储 |
| `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/knowledge/embeddings.py` | Embedding 协议、HTTP 适配及结果校验 |
| `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/knowledge/retrieval.py` | 关键词、余弦相似度与 RRF 混合排序 |
| `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/knowledge/service.py` | 检索问答、引用校验、追问查询构造 |
| `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/knowledge/cli.py` | 本地导入和显式索引同步命令 |
| `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/knowledge/evaluation.py` | 离线检索指标、报告输出 |
| `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/api/knowledge.py` | 知识问答和来源读取路由 |

### 数据约定

- `Document`：`document_id`、`source_path`（根目录内相对路径）、`source_url`（可空）、`title`、`content_hash`。
- `Chunk`：`chunk_id`、`document_id`、`document_version`、`heading_path`、`start_line`、`end_line`、`text`、`token_count`。
- `document_id` 由知识库标识和规范化相对路径计算稳定哈希；`chunk_id` 由文档 ID、内容版本、章节和序号计算。修改后保留文档身份，替换片段版本。
- `RetrievalHit`：`chunk`、`score`、`retrieval_method`。不同检索方法的原始分数不得直接相加。
- `Citation`：`citation_id`、`chunk_id`、文档标题、URL、章节、行号、证据摘录、文档版本。回答时保存证据快照，文章后续更新不破坏历史引用。
- `KnowledgeAnswer`：`answer`、`citations`、`evidence_status`（`supported`、`insufficient`、`unverified`）、`retrieval_query`。`supported` 表示模型按引用格式作答且引用 ID 有效，不等同于程序已经证明语义正确。
- `EmbeddingProvider.embed(texts: list[str]) -> list[list[float]]` 为异步接口；禁止空向量、非有限数字、数量不匹配或维度漂移。
- `KnowledgeRetriever.search(query: str, top_k: int, mode: str) -> list[RetrievalHit]` 为异步接口；`mode` 仅接受 `keyword`、`vector`、`hybrid`。

### 检索选型

第一版使用 SQLite 保存元数据和向量 JSON，小规模语料采用精确余弦扫描，记录实际片段规模和耗时。关键词基线使用标准库实现英文标识符分词、中文相邻双字切分及 BM25；固定 `k1=1.2`、`b=0.75` 作为起点。混合排序使用 RRF：每路前 20 个结果，分数为各路 `1/(60+rank)` 之和，去重后返回前 5 个；所有参数写入实验配置。

这些值是可复现的初始配置，不是已验证最优值。数据规模使扫描延迟无法满足实际体验后，再比较专用索引；不能把此方案描述为大规模向量搜索基础设施。

## 5. 实施任务

### 任务 1：打通会话历史，明确串行调用边界

**修改文件：**

- `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/agent/runtime.py`
- `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/agent/context.py`
- `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/sessions/repository.py`
- `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/llm/compatible_client.py`
- `/home/abeltomato/workspace/projects/Tomato-Agent/backend/tests/test_runtime.py`
- `/home/abeltomato/workspace/projects/Tomato-Agent/backend/tests/test_context.py`
- `/home/abeltomato/workspace/projects/Tomato-Agent/backend/tests/test_compatible_client.py`

- [ ] 运行上述三个测试文件的现有基线。
- [ ] 增加测试：同 Session 第二个 Run 能看到第一轮用户与最终回答；另一个 Session 看不到；恢复当前 Run 不重复消息。
- [ ] 新 Run 仅继承已完成 Run 的用户输入和最终回答；排除工具中间消息、失败和暂停 Run，按稳定持久化顺序读取。当前 Run 单独回放完整事件，避免重复拼接。
- [ ] 明确摘要计数对应的消息范围，恢复时保持一致；Context 裁剪按完整工具调用/结果组保留，不能留下孤立 tool 消息。
- [ ] Provider 请求设置串行工具选项；仍收到多个工具调用时返回明确协议错误。测试“两个工具调用不会悄悄只执行第一个”。
- [ ] 增加真实 HTTP 集成测试：连续发送两次消息，用记录输入的 FakeLLM 检查历史传递；临时数据库隔离。
- [ ] 更新 `/home/abeltomato/workspace/projects/Tomato-Agent/docs/系统设计.md` 的会话边界和串行限制。

**可直接加入现有 Runtime 测试文件的首个回归用例：**

```python
@pytest.mark.asyncio
async def test_new_run_receives_previous_completed_turn(tmp_path):
    llm = FakeLLM(
        LLMResponse(kind="final", content="你的博客主题是 Redis。"),
        LLMResponse(kind="final", content="可以继续讨论 Redis 持久化。"),
    )
    runtime, repository, session_id = await make_runtime(tmp_path, llm)
    first = await runtime.run(session_id, "我的博客主题是 Redis。")
    second = await runtime.run(session_id, "继续这个主题。")

    assert first.status == "completed"
    assert second.status == "completed"
    contents = [message.content for message in llm.messages[1][0]]
    assert "我的博客主题是 Redis。" in contents
    assert "你的博客主题是 Redis。" in contents
    assert contents.count("继续这个主题。") == 1
    other_session = await repository.create_session()
    llm.responses.append(LLMResponse(kind="final", content="新的会话。"))
    await runtime.run(other_session, "你好。")
    other_contents = [message.content for message in llm.messages[2][0]]
    assert "我的博客主题是 Redis。" not in other_contents
```

**验收：** 连续追问有历史，跨会话隔离，恢复不重复，多调用不静默丢失。

### 任务 2：建立语料清单与评测规范

**新增文件：**

- `/home/abeltomato/workspace/projects/Tomato-Agent/backend/tests/fixtures/knowledge/redis.md`
- `/home/abeltomato/workspace/projects/Tomato-Agent/backend/tests/fixtures/knowledge/cache.md`
- `/home/abeltomato/workspace/projects/Tomato-Agent/backend/tests/fixtures/knowledge/manifest.json`
- `/home/abeltomato/workspace/projects/Tomato-Agent/backend/tests/test_knowledge_dataset.py`
- `/home/abeltomato/workspace/projects/Tomato-Agent/docs/评测/评测规范.md`

- [ ] 编写短小固定语料，包含中文标题、英文 API 标识符、代码围栏、同名章节和一段伪造指令；全部为测试内容，不冒充真实博客。
- [ ] 清单固定为 JSON 数组，每项包含 `path`、`title`、`url`；限制相对路径和 `http/https` URL，URL 可空。
- [ ] 数据校验单测覆盖重复路径、目录穿越、非法 URL、缺少文档和无效行范围。
- [ ] 明确问题字段：`id`、`query`、`category`、`split`、`answerable`、`relevant_spans`、`reference_answer`；证据使用文档 ID、版本和行范围，不依赖某种分块方法的 chunk ID。
- [ ] 拿到真实博客后，人工标注 20–30 个问题，写入 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/evals/blog_questions.jsonl`；至少包含术语、改写、跨文章、无答案四类。数量是首批目标，不是当前已有数据。
- [ ] 按主题分组划分开发集和保留测试集，避免相似问题跨集合泄漏；真实语料不足时如实记录，不用自动生成重复问题凑数。

**验收：** 固定测试语料可离线使用；真实数据验收必须有真实问题与人工证据标注。

### 任务 3：Markdown 分块与可重复导入

**新增：** 第 4 节的 `models.py`、`ingestion.py`、`repository.py`、`cli.py`；测试 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/tests/test_knowledge_ingestion.py`、`/home/abeltomato/workspace/projects/Tomato-Agent/backend/tests/test_knowledge_repository.py`。

- [ ] 先测：同清单导入两次无重复；单文档修改只替换对应片段；导入失败保留旧版本；新增表不破坏已有会话。
- [ ] 只支持 UTF-8 Markdown、ATX 标题和反引号/波浪线围栏；围栏内的 `#` 不作为标题。第一版不解释 YAML front matter，元数据以清单为准。
- [ ] 正文按章节、段落分块，目标 500 tokens、普通块上限 800 tokens；携带标题路径和行号。大代码块作为独立块保留，超过单块 2,000 tokens 时显式报错并记录文档，不静默截断。
- [ ] 文档与片段更新使用事务；Embedding 索引设置为待更新，不把旧向量绑定到新文本。
- [ ] 实现 CLI `python -m app.knowledge.cli ingest --manifest PATH --root PATH`；默认不删除缺失文件。`sync --dry-run` 只列出候选移除项，实际移除必须显式确认操作范围。
- [ ] 测试符号链接逃逸、非法编码、空文档、中文行号、代码块和标题切分。导入报告输出成功、跳过、失败数量及失败原因。
- [ ] 新增 `/home/abeltomato/workspace/projects/Tomato-Agent/docs/知识库使用说明.md`，说明格式、限制、导入命令和删除语义。

**验收：** 相同输入结果稳定，更新原子化，异常可定位，原文来源不丢失。

### 任务 4：Embedding 与三种检索基线

**新增：** 第 4 节的 `embeddings.py`、`retrieval.py`；测试 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/tests/test_embeddings.py`、`/home/abeltomato/workspace/projects/Tomato-Agent/backend/tests/test_retrieval.py`。

**修改：** `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/settings.py`，以及任务 3 的知识库仓储与 CLI。

- [ ] 增加独立 `embedding_api_key`、`embedding_base_url`、`embedding_model`、`embedding_dimensions` 配置；不默认假定聊天服务支持 Embedding。
- [ ] 用 `httpx.MockTransport` 测试批量返回顺序、HTTP 失败、空向量、维度漂移和 NaN；凭据不得进入错误内容。
- [ ] 实现协议和 `/embeddings` HTTP 适配器；索引保存模型、维度、文本哈希和分块版本，查询配置不兼容时返回“需要重建索引”，不混用模型向量。
- [ ] 关键词基线测试精确标识符、中文词组、空查询；向量基线使用人工构造向量验证余弦排序，不在单测访问外部模型。
- [ ] 实现三种检索模式、RRF 去重、稳定并列排序；无结果返回空列表。关键词零命中文档不作为有效命中。
- [ ] 向量不可用时默认显式报错；如用户选择关键词降级，响应标记实际方法，不把降级结果记作混合检索。
- [ ] 真实 Embedding 验收单独执行，记录模型、语料规模、索引耗时和费用；更新知识库使用说明中的配置和重建步骤。

**验收：** 离线排序可预测；同一次查询能明确追踪模型、索引版本和方法。

### 任务 5：带引用的问答与追问 API

**新增：** 第 4 节的 `service.py`、`/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/api/knowledge.py`；测试 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/tests/test_knowledge_service.py`、`/home/abeltomato/workspace/projects/Tomato-Agent/backend/tests/test_knowledge_api.py`。

**修改：** `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/main.py`，现有 Runtime 和模型文件（仅增加复用的会话与返回字段）。

- [ ] 定义 `POST /api/sessions/{session_id}/knowledge-runs`，请求为 `message` 和 `retrieval_mode`，返回兼容 `RunResult` 的字段及 `citations`、`evidence_status`。
- [ ] 使用该 Session 已完成轮次构造独立检索问题；记录原问题和改写问题。首轮不改写，追问改写调用失败则报告错误，不静默吞掉上下文。
- [ ] 按固定流程“查询→检索→生成”执行；模型输出 JSON，包含 `answer`、引用 ID 列表和证据状态，用 Pydantic 校验。未命中时不调用生成模型，返回材料不足。
- [ ] 仅允许引用本次检索获得的片段；模型虚构 ID 或输出格式错误时返回可识别失败，不展示伪引用。引用条目存在并不自动证明结论正确。
- [ ] 提示词要求区分有依据结论与材料缺口；资料中的指令不得改变工具权限。用包含恶意指令的测试文档验证工具层始终不开放写操作。
- [ ] 使用现有 Session/Run/Event 表持久化输入、最终回答、检索方法与引用快照；新 Run 可继承知识问答的最终消息。
- [ ] 提供 `GET /api/sessions/{session_id}/messages` 与 `GET /api/knowledge/documents/{document_id}`；文档 ID 经仓储解析，接口不得接受任意文件路径。
- [ ] HTTP 集成覆盖未知 Session、空问题、无结果、虚构引用、连续追问、来源更新后的历史快照；真实模型语义质量留给任务 7。
- [ ] 更新系统设计和知识库使用说明中的 API、引用语义、错误码与外部数据流向。

**验收：** 从 HTTP 输入到存储和来源查看形成闭环；自动验证格式及引用归属，人工验证语义支撑。

### 任务 6：知识助手前端

**修改：** `/home/abeltomato/workspace/projects/Tomato-Agent/frontend/src/main.tsx`、`/home/abeltomato/workspace/projects/Tomato-Agent/frontend/src/style.css`。

**新增：** `/home/abeltomato/workspace/projects/Tomato-Agent/frontend/src/api.ts`、`/home/abeltomato/workspace/projects/Tomato-Agent/frontend/src/components/CitationList.tsx`。

- [ ] 把请求类型与地址配置集中到 `api.ts`；消息状态区分请求中、成功、失败和证据不足。
- [ ] 展示来源标题、章节、摘录和原文链接；无外部 URL 时打开内部文档。只允许安全 URL，正文按纯文本呈现，不直接注入 HTML。
- [ ] 请求中禁用重复发送；会话 ID 本地保存，刷新后从后端加载历史，失效 Session 有明确重建入口。
- [ ] 保留基础聊天入口，知识模式调用任务 5 的 API；禁止把 Mock 搜索标记为真实来源。
- [ ] 为新增后端返回格式补充 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/tests/test_knowledge_api.py` 契约测试；前端目前未声明测试框架，本期不为了静态展示新增框架。
- [ ] 执行 TypeScript 类型检查，并人工走查首轮、追问、刷新、失败、空来源、来源点击和重复发送；记录结果到知识库使用说明的验收区。

**验收：** 用户能辨认答案、证据和错误，不需要查看数据库才能核验来源。

### 任务 7：评测、优化与版本 A 交付

**新增：** 第 4 节的 `evaluation.py`；测试 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/tests/test_knowledge_evaluation.py`；报告 `/home/abeltomato/workspace/projects/Tomato-Agent/docs/评测/博客RAG基线报告.md`。

- [ ] 指标单测采用手算结果：相关证据中命中 1/2 时 Recall=0.5；首个相关结果排名 2 时 RR=0.5；无结果时为 0。
- [ ] 按证据段计算 Recall@5、MRR@5，命中要求相同文档版本且检索片段覆盖标注行范围。无答案样本不纳入召回分母，单独统计正确拒答和错误作答。
- [ ] CLI 固定为 `python -m app.knowledge.evaluation --dataset PATH --split dev --mode keyword --output PATH`，模式也支持 `vector`、`hybrid`；同一语料快照、同一划分运行三次对比。
- [ ] 报告记录问题数、类别、语料哈希、模型、参数、检索延迟和答案人工评估；真实端到端耗时与离线检索耗时分开。模型未提供 usage 时标记未知，不伪造精确费用。
- [ ] 人工检查引用是否支持主要结论、是否遗漏关键限定、无答案时是否编造；机器评分仅作为辅助。
- [ ] 只在开发集调分块和 top-k，保留集用于最终报告；若查看保留集后继续调参，明确标记该集合不再是独立测试集。
- [ ] 先记录 baseline，再依据具体失败类型决定是否加入重排；重排作为额外切片，需单独明确 Provider、测试和成本，不能跳过基线直接宣称收益。
- [ ] 报告至少列出 5 个代表性失败样例、归因和下一步；提升不显著或退化时照实写。
- [ ] 更新 `/home/abeltomato/workspace/projects/Tomato-Agent/README.md`：可运行步骤、演示案例、能力限制、评测链接；更新 `/home/abeltomato/workspace/projects/Tomato-Agent/docs/实施计划.md`，标记旧计划为历史并链接本文。

**验收：** 能用同一套配置复现实验；不设置未经测量的“准确率提升 30%”之类目标，不用合成单测成绩代表真实效果。

### 任务 8：将知识检索接入 Agent 工具

**新增：** `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/tools/search_knowledge.py`、`/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/tools/read_knowledge.py`；测试 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/tests/test_knowledge_tools.py`。

**修改：** `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/main.py` 和 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/tests/test_runtime.py`。

- [ ] `search_knowledge(query, top_k)` 复用任务 4 的检索器，返回片段 ID、摘录和版本；`read_knowledge(chunk_id)` 根据仓储读取受限片段，不接收文件路径。
- [ ] 按现有工具模式实现 `definition` 和 `execute`，工具输入由 Pydantic 校验，结果长度受预算限制且保留来源标识。
- [ ] 用 FakeLLM 测试“搜索→读取→回答”、未知 ID、工具失败后停止或改问、预算耗尽；当前版本不注册联网 Mock 搜索作为有效资料来源。
- [ ] Agent 最终引用只能来自当前任务已读到的片段；复用任务 5 的引用归属验证。
- [ ] 系统设计说明固定 RAG 与自主工具选择的边界，并在评测规范增加工具轨迹检查方式。

**验收：** 可查看实际工具轨迹；增加工具选择不会破坏版本 A 的确定性问答入口。

### 任务 9：技术写作状态机、确认与幂等保存

**新增模块：** 在 `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/writing/` 下创建 `__init__.py`，以及以下文件：

- `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/writing/models.py`
- `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/writing/repository.py`
- `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/writing/service.py`
- `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/api/writing.py`
- `/home/abeltomato/workspace/projects/Tomato-Agent/backend/tests/test_writing_workflow.py`
- `/home/abeltomato/workspace/projects/Tomato-Agent/backend/tests/test_writing_api.py`

**修改：** `/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/main.py`、`/home/abeltomato/workspace/projects/Tomato-Agent/backend/app/settings.py`。

- [ ] 状态固定为 `researching → awaiting_outline_confirmation → drafting → awaiting_save_confirmation → saved`，另有 `failed`；服务端控制迁移，模型文本不能直接改变状态或表示用户批准。
- [ ] 每个写作任务保存 `task_id`、`session_id`、`status`、`version`、提纲、草稿、引用快照、关联 Run ID、失败阶段；各生成阶段使用独立 Run，已完成 Run 不重新执行。
- [ ] API：`POST /api/sessions/{session_id}/writing-tasks` 创建；`GET /api/writing-tasks/{task_id}` 查询；`POST /api/writing-tasks/{task_id}/confirm-outline` 提交 `version` 与确认后的提纲；`POST /api/writing-tasks/{task_id}/save` 提交 `version` 和 `idempotency_key`。
- [ ] 提纲包含已支持内容、缺少材料及引用；用户可修改后确认。无足够材料时保留缺口说明，不伪造检索成功。
- [ ] 数据库以条件更新确保预期状态和版本匹配；过期确认或并发操作返回 409。重复相同保存请求返回原结果，不生成多份草稿。
- [ ] 保存仅允许配置的草稿目录，文件名由 task_id 和草稿版本生成，不接受任意目标路径、不覆盖已有不同内容；使用同目录临时文件加原子替换，并处理“文件已生成但数据库未标记”的重试。
- [ ] 测试未确认不能生成草稿、未确认不能保存、模型伪造确认无效、过期版本、并发确认、重复保存、进程重启后的暂停任务加载和保存故障恢复。
- [ ] 不承诺正在执行的任意工具自动恢复。新增 `POST /api/writing-tasks/{task_id}/retry`，仅允许 `failed` 且只读生成阶段失败的任务重试；保存阶段通过原幂等请求重试，写入前核对内容哈希。
- [ ] 新增 `/home/abeltomato/workspace/projects/Tomato-Agent/docs/技术写作Agent说明.md`，写清 API、状态迁移、恢复范围和保存目录权限。

**验收：** 人工确认是后端约束；暂停任务重启后可继续；重复请求不重复产物。数据库和文件系统之间不宣称分布式事务或恰好一次执行。

### 任务 10：写作交互、集成验收与求职材料

**新增：** `/home/abeltomato/workspace/projects/Tomato-Agent/frontend/src/components/WritingTaskPanel.tsx`、`/home/abeltomato/workspace/projects/Tomato-Agent/backend/tests/test_writing_integration.py`。

**修改：** 前端入口与 `/home/abeltomato/workspace/projects/Tomato-Agent/frontend/src/api.ts`；README 与技术写作 Agent 说明。

- [ ] 前端展示材料、缺口、可编辑提纲、草稿预览、确认按钮和阶段错误；查询任务状态恢复页面，确认时携带版本，按钮点击后避免重复提交。
- [ ] 跨模块集成测试使用临时语料、数据库、草稿目录、Fake Embedding 和 FakeLLM，完整执行“导入→检索→提纲→确认→草稿→确认保存→重复保存”。
- [ ] 测试草稿保存前目录无输出文件、保存后正文与预览一致、重复保存只有一个目标文件；真实模型的文案质量不由 Fake 集成测试证明。
- [ ] 执行一次真实使用验收，记录任务完成情况、工具调用次数、生成耗时、人工修改点和失败案例；如没有外部服务条件，明确标记真实验收未完成。
- [ ] 录制短演示：知识追问与引用、材料不足、写作确认与保存；文档说明运行方式、数据规模、评测方法和已知限制。
- [ ] 简历仅写实际完成和验证的能力；指标引用报告中的真实数值，不提前填写百分比、并发能力或大规模数据量。

**验收：** 可从干净的临时数据环境重现 A/B 两个闭环，且能解释每个关键取舍。

## 6. 验证命令与范围

以下命令供实施任务时使用，本次编写计划不执行应用测试。后端工作目录为 `/home/abeltomato/workspace/projects/Tomato-Agent/backend`；使用当前已有虚拟环境，不自动安装依赖。

```bash
cd /home/abeltomato/workspace/projects/Tomato-Agent/backend
.venv/bin/python -m pytest tests/test_runtime.py tests/test_context.py tests/test_compatible_client.py tests/test_main.py -q
```

第一条用于任务 1；后续按下表选择文件，替换上面 `pytest` 后的测试参数，不自动运行全项目套件。

| 任务 | pytest 文件参数（相对上述工作目录的命令参数） |
|---|---|
| 2 | `tests/test_knowledge_dataset.py` |
| 3 | `tests/test_knowledge_ingestion.py tests/test_knowledge_repository.py` |
| 4 | `tests/test_embeddings.py tests/test_retrieval.py` |
| 5–6 | `tests/test_knowledge_service.py tests/test_knowledge_api.py tests/test_runtime.py` |
| 7 | `tests/test_knowledge_evaluation.py tests/test_retrieval.py` |
| 8 | `tests/test_knowledge_tools.py tests/test_runtime.py` |
| 9 | `tests/test_writing_workflow.py tests/test_writing_api.py` |
| 10 | `tests/test_writing_integration.py tests/test_writing_workflow.py tests/test_writing_api.py` |

前端变更后执行：

```bash
cd /home/abeltomato/workspace/projects/Tomato-Agent/frontend
./node_modules/.bin/tsc --noEmit -p /home/abeltomato/workspace/projects/Tomato-Agent/frontend/tsconfig.json
```

API 集成测试采用 `httpx.ASGITransport`，不依赖占用本机端口。涉及启动生命周期的用例需显式初始化和关闭资源。单元测试、HTTP 集成、真实模型验收分别报告，不能互相替代。

每个切片结束时记录：修改文件、验证命令、通过数/总数、失败项、文档回填；未执行检查标记“未执行”，不写“已通过”。记录在对应功能或评测文档中，不新增开发日志。

## 7. 风险与停止扩张条件

| 风险 | 应对和边界 |
|---|---|
| 博客少、主题单一，RAG 优势不明显 | 加入关键词或可容纳的全文上下文基线，如实报告，不为证明架构价值造数据 |
| 中文切分影响关键词召回 | 固定分词规则并分析漏召回，再决定是否增加专用分词依赖 |
| 引用存在但不支持结论 | 区分格式校验与语义正确性，人工评估支持程度 |
| Embedding 服务不可用或成本超预算 | 离线 Fake 只验证工程逻辑；关键词模式可继续使用，真实效果验收保持未完成 |
| 数据更新使评测标注失效 | 固定语料快照与文档版本；更新后重新核对标注 |
| 工具超时、取消或崩溃导致重复执行 | 先保证只读工具；写作保存采用独立确认与幂等键，不泛化为任意工具可靠恢复 |
| 现有总耗时预算不是强制中断 | 在版本 B 接入时检查模型调用剩余预算，使用异步超时约束并测慢模型；超时应保留失败阶段 |
| 界面、动画挤占核心开发时间 | M2 前只做聊天和来源，M3 前只做任务确认；不提前做复杂桌面形象 |

## 8. 后续扩展门槛

1. **MCP：** M2 检索稳定后，将 `search_knowledge` 和 `read_knowledge` 作为对外工具，单独确定协议 SDK、安全边界和兼容性测试；不替代内部检索服务。
2. **桌面悬浮形象：** M3 之后明确目标操作系统，再比较 Electron/Tauri；优先快捷唤起、状态展示和用户主动提交选中文本，单独验证 Python 后端分发。
3. **长期记忆：** 有稳定日常使用后，再设计可查看、修改、删除的偏好与任务记忆；知识库、会话历史和个人记忆分开存储与评估。
4. **重排或专用向量索引：** 由召回失败和性能测量触发；有对照实验后才增加复杂度。

## 9. 计划完成检查

- [ ] M0：连续会话测试通过，固定语料和评测规范存在。
- [ ] M1：真实博客导入、来源引用和追问通过人工验收。
- [ ] M2：完成三种检索策略对比，报告包含失败样例和真实限制。
- [ ] M3：确认前不执行对应动作，保存幂等，完整集成测试通过。
- [ ] 文档与实际接口一致，旧计划明确标记历史。
- [ ] 简历与演示只声称已实现、已验证的能力。
