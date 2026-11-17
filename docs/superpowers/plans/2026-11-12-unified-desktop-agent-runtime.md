# 桌面端统一 Agent Runtime 实施计划

> 状态：阶段 0–6 已完成（2026-11-13）。阶段 3/4/6 已通过 Windows WebView2 → Tauri → Python Runtime → Rust Broker → NTFS 的真实桌面闭环：历史迁移、Run 级读取授权、项目读取、ChangeSet 差异审阅、显式批准、刷新恢复、应用进程中断后重启恢复、安全落盘与 Runtime 完成。探针还验证引擎按需启动且在同一桌面进程内复用；外部冲突不会覆盖文件，并显示 `workspace_conflict`；只读目标文件导致写入失败时，UI 显示失败提示和 journal 状态，原内容保持不变。Linux 与 Windows/NTFS Broker 测试覆盖路径/链接边界、ACL 读取拒绝、冲突预检、原子替换、部分提交及 journal 恢复；Python API 覆盖拒绝提案零写入和提交回执恢复；UI 覆盖审批/拒绝及同 Run 连点保护。阶段 5 的 Protocol 与不可用命令后端契约已完成，首版不提供真实命令执行。迁移成功后 Python 是唯一会话和消息读写源；迁移失败时 Rust 旧库保持活动源。
> 设计依据：[桌面端统一 Agent Runtime 设计](../specs/2026-11-12-unified-desktop-agent-runtime-design.md)。

## 目标与范围

把桌面端普通聊天与任务入口合并到一个 Python Runtime 和单一输入框。Python 拥有可见消息、Session、Run 和事件；Rust 负责桌面 UI、项目身份、本机凭据和受控文件操作。首个完整闭环是：用户为当前 Run 授权读取项目 → Runtime 理解项目并产生结构化 ChangeSet → 桌面显示完整 diff → 用户审批 → Rust 按审批安全落盘。

本计划不实现通用 CLI、不实现真实 Docker 或 Windows 沙箱、不合并知识/写作/代码任务的领域状态机，也不提供宿主机执行回退。执行后端接口先冻结并由 `UnavailableExecutionBackend` 实现。

## 设计约束

- 所有会话输入均经同一 Python Runtime 创建一个 Run；删除 UI 模式选择和普通聊天/任务分流。
- Rust 聊天历史迁入 Python 后只保留单一活动写源。迁移失败时旧 Rust 数据库仍是活动源；迁移成功后旧库只读保留。
- 项目读取授权绑定 session、Run、项目固定身份、只读操作范围和授权版本，用户每个 Run 重新授权。前端选择本身不构成授权。
- Python 不持有项目绝对路径，不直接读写项目目录；项目操作经 Rust WorkspaceBroker 的私有认证通道完成。
- ChangeSet 首版只支持新增和修改；diff、内容摘要、项目/Run、授权版本和目标旧哈希参与审批绑定。删除、重命名、路径逃逸、链接越界与冲突一律拒绝。
- 应用前重新核验审批票据和旧哈希；逐文件原子替换、持久化提交日志；明确呈现多文件部分成功，不承诺整体原子性或总能回滚。
- 所有恢复路径以持久事实为准。副作用状态未知时先核查，不自动重放。

## 主要代码位置

- Python Runtime/API/存储：`backend/app/agent/`、`backend/app/api/sessions.py`、`backend/app/sessions/repository.py`、`backend/app/dependencies.py`、`backend/app/engine.py`。
- Rust 桌面宿主：`desktop/src-tauri/src/engine.rs`、`desktop/src-tauri/src/chat/{store.rs,commands.rs,runtime.rs,models.rs}`。
- UI：`desktop/ui/src/desktop-chat/DesktopChatApp.tsx`、`desktop/ui/src/style.css`、`desktop/ui/tests/`。
- 现有验证入口：`backend/tests/test_assistant_api.py`、`test_assistant_persistence.py`、`test_unified_tasks_api.py`、`desktop/src-tauri/src/chat/tests.rs`、`desktop/ui/tests/desktop-chat-entry.test.mjs`、`desktop/ui/tests/desktop-projects.test.mjs`。

## 阶段 0：锁定协议和数据库迁移前置条件（已完成，2026-11-12）

1. 在 Python 中定义版本化桌面能力桥 schema：请求 ID、协议版本、实例 ID、Run/session/project 身份、授权票据、操作类别和有界参数；响应采用稳定错误码和结构化结果。定义 `WorkspaceBroker` 与 `ExecutionBackend` Protocol，不让业务 Runtime 依赖传输实现或具体执行器。
2. 定义 `BackendCapabilities`、`ExecutionHandle`、`ExecutionResult`、取消/核查/清理结果及稳定状态枚举；命令使用结构化 `argv`，不接受 shell 字符串；`UnavailableExecutionBackend` 明确拒绝 prepare/execute。
3. 在 Rust 中复用 `EngineManager` 的实例认证原则建立私有双向 RPC。认证绑定该桌面进程实例与引擎实例，含请求 nonce/重放拒绝、连接生命周期和消息大小上限；WebView 不取得 broker 凭据，HTTP 公开 API 不转发 broker 方法。
4. 为协议和启动失败添加契约测试：版本不匹配、跨实例、缺失 session/Run/project/grant 身份、nonce 重放、超限消息和不可用后端均拒绝或给出稳定失败；Run/project 是否属于授权项目由阶段 3 的授权仓储验证；证明首版无本机命令 fallback。

验收：Python/Rust 协议版本明确；假 broker/backend 可独立替换；请求必须通过宿主的协议与实例校验；不可用后端报告能力不可用且不创建执行副作用。已完成版本化 Python `ExecutionBackend` / `WorkspaceBroker` 契约、明确拒绝执行的后端、Rust 生命周期内的本机桥监听器和 Python 客户端。监听器目前对所有项目操作返回 `broker_unavailable`；session/Run/project 已进入协议，但其与真实授权记录的关系留在阶段 3 验证，文件操作留在阶段 3/4；没有命令执行 fallback。

## 阶段 1：将 Rust 聊天历史迁到 Python 权威库（已完成，2026-11-13）

1. 扩展 Python schema 和 repository，增加桌面可见 `messages` 表，保留消息 ID、session ID、角色、正文、创建时间和顺序；为 assistant 消息增加 `(run_id, message_kind)` 等幂等关联。旧 Agent 事件继续作为执行/恢复事实，不合成为伪历史 Run。
2. 新增只允许桌面本机调用的迁移预检、批次导入、校验和切换 API。迁移批次按 Rust installation ID 幂等；单批消息导入事务化。保存源记录数、ID 集合摘要和批次状态，支持中断后继续。
3. Rust 侧读取当前 `sessions/messages/engine_sessions`，按原 ID 导入 Python。项目根路径仍仅由 Rust 项目注册表持有；将原 session `project_id` 映射为 Python session 的 project association。定义历史 session ID 保留策略，避免旧的 engine session mapping 造成双身份。
4. 切换前逐项核对 session/message 数、ID、角色、时间、正文摘要、项目关联和每个会话内顺序。验证失败不切换，也不删改 Rust 数据；用户继续使用旧库并获得明确错误。
5. 核验成功后切换 UI 的会话列表、创建、选择、重命名、删除和历史读取为 Python API；发送消息的每条 Run 同时持久化用户/assistant 可见消息与事件。Rust 数据库只读保留，禁止双写。
6. 增加重启/迁移恢复测试，覆盖空库、中文和长消息、重复迁移、导入中断、摘要不符、project ID 缺失/失效、已有 Python session ID 冲突及切换标志写入失败。

验收：Rust 历史 ID 和可见内容一致；迁移重复执行无重复记录；中断能续跑；任何校验失败均保持旧源活动；迁移成功后每条新输入仅在 Python 产生一条用户消息、一个 Run 和至多一条最终回复。

## 阶段 2：统一桌面输入、消息与 Run 状态（已完成，2026-11-13）

1. 在 Python Runtime 增加统一用户消息入口，原子提交 user message 与 Run 起始事件，并以 client request ID 幂等去重。助手最终消息与 Run 完成状态以同一幂等键提交。
2. 提供可见消息分页 API 和 Run 事件游标 API。消息排序使用明确序号/创建时间及 ID 稳定打破并列；事件 API 返回 `next_cursor`，UI 用游标补齐断线期间事件。
3. 将 `DesktopChatApp.tsx` 的模式 state、模式按钮、模式相关提示和 `desktop_chat_send` / `desktop_agent_submit_task` 分支替换成单一 submit/cancel Runtime 命令。保留任务历史只读兼容，领域动作不会因此被改写。
4. 流式响应从 Python 持久事件刷新；只在事件确认后显示可恢复内容。刷新、切换会话或引擎重启后从 Python 消息/事件重建页面，不依赖易失 Rust 内存状态。
5. 为重复 request ID/相同内容、重复 ID/不同内容、并发 session 请求、取消、Provider 失败、断连恢复、事件游标分页、重复最终事件和旧 Rust 聊天入口禁用增加测试。

验收：一个输入只有一条入口和一个 Run；简单回答仅显示一次；断线刷新不丢消息、不重复最终回复；UI 无聊天/助手/写作模式切换；知识/写作/代码任务既有隔离流程仍可被后续适配。

## 阶段 3：项目只读授权与 WorkspaceBroker（已完成，2026-11-13）

1. 建立 Rust 项目固定身份和根路径解析：授权对象引用项目 ID 与已登记根目录版本，不接受 Python/模型传入的任意绝对根路径。项目不可用、目录身份变化或授权版本变化时拒绝。
2. 实现 Run 级一次性读取授权与授权状态查询/撤销；授权范围仅含列目录、搜索文本、读取文件。Broker 每次请求校验 session、Run、project、授权版本、操作和资源预算，拒绝旧 Run/跨项目/过期票据。
3. 在 Rust 中实现 `list_entries`、`search_text`、`read_file`：相对路径规范化、深度/数量/字节数上限、忽略或拒绝敏感目录策略、有界文本返回。项目内容作为不可信数据交给模型。
4. 做 Windows 目录边界检查，覆盖绝对路径、`..`、大小写/分隔符混淆、symlink/junction/reparse point、重解析竞态、越界硬链接可行性、ACL 拒绝及根目录被替换。实现使用目录句柄逐段打开、拒绝所有 reparse 属性、句柄相对枚举；原生 Windows/NTFS 已验证项目内与父路径 junction/reparse 拒绝、硬链接读取拒绝、ACL 读取拒绝及句柄枚举。真实 WebView 闭环已验证授权后读取已登记项目；应用级竞态覆盖仍有限，Linux 单测不能替代 Windows 运行测试。
5. UI 在读取前显示当前项目与只读范围，用户明确确认后才发出本 Run 授权。授权取消/拒绝作为 Runtime 事件持久化，不由模型再次扩大权限或自动重复请求。
6. 以 fake broker 测试 Python 消费结构化结果；以 Rust 临时目录测试宿主真实检查。增加授权期间 UI 切换/Run 取消与迟到 RPC 的竞态测试。

验收（已完成，2026-11-13）：Windows WebView IPC 实跑确认读取授权前等待用户操作，授权后通过已登记项目读取 README；Windows/NTFS Broker 测试验证越界、链接、ACL、旧 Run 和替换项目根目录拒绝。授权撤销、取消竞态由 Python/Rust 定向测试覆盖。

## 阶段 4：ChangeSet 提案、审批与安全落盘（已完成，2026-11-13）

1. [x] 定义 Python 严格 ChangeSet schema：change ID、Run/session/project、授权版本、相对路径、`add|modify`、预期旧哈希/目标不存在条件、完整旧/新内容、摘要、大小预算和 canonical digest。校验重复/冲突路径、旧内容与哈希对应、非法编码和超限 payload。
2. [x] Runtime 只产生提案并持久化 `changeset.proposed` 和不可变等待动作；Run 转入 `waiting`；Python 不触碰原始路径。生成内容通过 schema 和授权/项目绑定校验。
3. [x] UI 显示完整变更内容、摘要和逐行增删差异，可显式批准或拒绝；大文本超出差异预算时回退到完整并排内容。审批区是内联待操作状态，不是可关闭弹窗，离开页面不会被伪装成拒绝；Run 仍保留等待动作，可重新进入处理。关闭窗口的取消语义仍需后续明确。
4. [x] 用户批准时 Rust 从 Python 冻结等待动作读取提案，签发一次性票据绑定 ChangeSet digest、文件集合、项目身份、Run 与授权版本、有效期。拒绝/过期/重放返回可恢复错误；已覆盖审批票据过期与篡改提案拒绝。
5. [x] apply 前全量预检票据、项目绑定、目标路径、旧哈希/目标不存在、文件类型/大小及链接边界。任何预检冲突都不开始写入。
6. [x] 使用持久提交 journal 记录 intent、逐文件原子替换和应用状态。journal 校验 schema 和状态组合；重复提交先核验项目现场，部分提交只继续未应用文件，状态冲突时拒绝自动重放。UI 列出准确文件状态并提供继续/停止动作；不宣称跨多个文件原子性。Linux 与原生 Windows/NTFS 测试验证新增/修改原子提交、部分提交续跑，以及原子替换后逐文件状态落盘前中断时由新 Broker 恢复；Windows 原子替换和 journal 中断窗口已有实际运行证据。
7. 增加批准前外部修改冲突、拒绝零写入、篡改 digest、路径穿越、审批重放、同路径多变更、临时文件清理、单文件替换失败、多文件中途失败、崩溃重启核查和重复 RPC 测试。当前已覆盖冲突、篡改、路径/重复路径校验、重放、临时文件、单/多文件失败、子进程实际终止后的 Broker 恢复及宿主 `host_apply_changeset` 重复调用。Python Run API 测试覆盖拒绝提案后 sentinel 项目文件字节不变、有效宿主提交回执恢复等待 Run、重复动作拒绝；完整 UI→Tauri→Python 状态提交链仍待端到端覆盖。

验收（已完成，2026-11-13）：真实 WebView IPC 验证提案展示、刷新后恢复、显式批准及 README 安全更新；拒绝零写入、旧哈希冲突不覆盖、多文件部分状态和 Broker 进程中断恢复由 Python/Rust 测试覆盖。完整 Tauri 应用进程中断恢复及冲突/只读目标文件写入失败提示在阶段 6 的 Windows WebView 探针中实测通过。

## 阶段 5：ExecutionBackend 固定实现与契约套件（已完成，2026-11-13）

1. [x] 完成版本化接口：`describe_capabilities`、`prepare`、`execute`、`cancel`、`inspect`、`collect_changes`、`close`。工作区快照是只读、内容寻址输入；协议要求执行器创建隔离副本，不挂载或修改原项目。
2. [x] 固定输入输出与错误语义：结构化 argv/cwd/environment allowlist/timeout；有界参数、环境与输出；`completed|rejected|timed_out|cancelled|failed|unknown`；稳定错误码、invocation ID 和可核验清理结果。
3. [x] 提供 `UnavailableExecutionBackend` 作为首版装配，并用测试 fake 实现走完整契约。不可用实现不返回伪成功、不执行本机进程、不生成虚假隔离声明；Runtime 依赖 Protocol，`run_command` 能力明确列为不可用。
4. [x] 契约测试覆盖能力声明、参数验证、重复 invocation、超时、运行中取消、inspect unknown、输出截断、ChangeSet 导出、清理失败、崩溃未知状态和重复 close。

验收：Runtime 依赖 Protocol，不依赖 fake/真实后端类；命令工具显示明确不可用；后续 Docker/Windows 后端须先通过相同契约测试并独立完成隔离验收。

## 阶段 6：回归、Windows 验收与发布切换（已完成，2026-11-13）

1. 运行 Python 相关 API、Runtime、持久化、工作区及迁移测试；运行 UI 测试/build；运行 Rust 单测和 Tauri build 检查。按变更范围执行命令，记录精确结果。
2. 在真实 Windows/NTFS 环境完成桌面端端到端检查：
   - [x] WebView IPC 历史迁移、按需启动与引擎复用、Run 级只读授权、项目读取、diff 审批、刷新恢复待审批状态及批准落盘。
   - [x] Rust Broker 原生 NTFS 测试覆盖 reparse/junction/硬链接拒绝、外部冲突保护、ACL 读取拒绝、原子替换、部分提交和 Broker 子进程中断后的 journal 恢复。
   - [x] 在真实 Tauri 应用中强制中断 journal 提交窗口并重启应用，验证 UI/Runtime/Broker 联合恢复。
   - [x] 用真实 WebView 验收外部冲突与只读目标文件导致的落盘失败提示、journal 状态及文件内容保护。
3. 发布开关先以迁移完成标志控制消息读写源；旧 Rust DB 只读保留作为人工回退数据。不要在首版自动向旧库镜像写入或自动切回形成双写。
4. 复核日志与错误响应不包含 API key、完整敏感文件内容、broker token 或未授权绝对路径；检查 UI 通过 Tauri 命令访问 broker，不得直连私有 RPC。

最终验收：新建/旧会话统一输入均进入 Python Run；历史一致迁移且失败可回退；Run 级只读授权和审批写入闭环已通过真实 Windows WebView 验证；ExecutionBackend 协议有 conformance suite；首版没有通用 CLI 或宿主执行 fallback。阶段 6 的 Windows WebView 应用进程中断恢复、外部冲突提示和只读文件落盘失败提示均已通过真实运行验收。

## 建议命令

从仓库根目录运行，执行时按实际文件变更收窄或扩展：

```bash
cd backend && pytest -q tests/test_assistant_api.py tests/test_assistant_persistence.py tests/test_unified_tasks_api.py
cd desktop/ui && node --test tests/desktop-chat-entry.test.mjs tests/desktop-projects.test.mjs
cd desktop/ui && pnpm build
cd desktop/src-tauri && cargo test
cd desktop/src-tauri && cargo check
git diff --check
```

若 UI 当前测试依赖仓库提供的 Node 测试启动脚本，则沿用该脚本，不为计划改变测试工具链。Windows/NTFS 验收必须在 Windows runner 或产品环境执行，Linux 结果不可代替。

## 风险与实施顺序

- 阶段 0 的私有 IPC 与实例认证是阶段 3/4 的前置条件；不能用桌面普通 HTTP 端点代替 broker。
- 阶段 1 会跨越 Rust/Python 两套 SQLite，必须先交付可重入迁移与校验，再切换 UI 数据源。
- 阶段 4 的 journal 只能精确恢复和报告部分提交，无法提供跨多个文件的通用事务回滚。
- Windows reparse point 与替换语义具有平台差异；相关测试不得仅由 Linux 临时目录测试代替。
- 执行后端契约可在项目理解闭环之后并行实现 fake conformance tests，但真实执行器不在本计划的可交付范围。
- 每阶段完成后先运行针对性测试和代码审查，再进入依赖阶段；若仓库实现揭示设计接口需变更，先更新设计文档和本计划，再实现对应阶段。
