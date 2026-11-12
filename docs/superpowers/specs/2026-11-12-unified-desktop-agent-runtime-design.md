# 桌面端统一 Agent Runtime 设计

## 状态与范围

状态：用户已确认设计方向，待书面评审。本文定义桌面端普通聊天、复杂任务调度、项目理解与安全文件变更的目标架构；不表示实现已开始或验收已完成。

## 目标

- 普通聊天与复杂任务使用一个 Python Agent Runtime 和单一桌面输入框。每条用户消息创建统一 Run，由 Runtime 管理上下文、能力调度、等待、事件、取消和恢复。
- Python 引擎由 Rust 桌面宿主按需启动，并在桌面应用生命周期内复用。
- Rust 聚焦桌面 UI 宿主、本机凭据/文件能力和授权呈现；Python 引擎是会话、消息、Run 与运行事件的权威源。
- 首版形成“理解指定项目 → 生成修改提案 → 用户审批 → 安全落盘”的最小闭环。
- 现在冻结执行后端的逻辑协议，使后续 Docker 或 Windows 原生沙箱可独立实现并通过同一契约测试。

## 已确认边界

- 桌面不提供聊天、助手、写作等模式切换。所有输入先进入统一 Runtime。
- 当前 Run 可以获得用户明确授予的项目读取授权；授权绑定 Run、项目和操作范围，不跨 Run 继承。
- Runtime 只生成结构化变更集，不直接修改原项目。写入前必须在桌面显示精确 diff 并获得用户审批。
- 首版不执行通用 CLI，不因执行后端缺失而回退到宿主机执行。
- 知识检索/问答、写作、代码任务保留业务边界和各自状态机。本设计不重写其领域流程；后续可通过能力/工作流适配器接入统一调度。
- 既有 Rust 聊天会话和消息迁入 Python 引擎，保留标识并验证后切换；迁移成功前原数据库作为只读回退副本。
- Docker、镜像工具链供应、Windows 容器 UID 映射和 Windows 原生沙箱均不属于首版闭环交付。

## 组件职责

### 桌面 UI

- 提供单一输入框、会话导航、Run 状态和持久事件时间线。
- 展示只读项目授权请求和逐文件 ChangeSet diff，提交审批或拒绝。
- 只通过 Tauri 命令与 Rust 宿主交互；不得直接持有 Python 引擎凭据或私有能力桥凭据。

### Rust 桌面宿主

- 启动、健康检查、复用和停止 Python 引擎；管理模型凭据与桌面应用生命周期。
- 拥有桌面项目注册表和 WorkspaceBroker。校验会话/Run/项目授权后，执行受限的列目录、搜索、读取和变更应用。
- 管理审批界面与本地授权状态，拒绝 WebView 直接访问私有 broker。
- 对 ChangeSet 执行路径校验、哈希冲突检查、原子文件写入及提交日志管理。

### Python Agent Runtime

- 拥有 Session、用户可见 Message、统一 Run、能力调用和可恢复事件。
- 每条输入都建立 Run，并统一执行模型决策、上下文管理、工具调度、等待用户动作、取消与恢复。
- 通过私有、认证的桌面能力桥调用 WorkspaceBroker；授权范围由桌面宿主发放并绑定 Run，模型输出不能扩大授权。
- 生成 ChangeSet 并将其绑定 Run 和授权请求。审批完成后发起应用请求，不直接访问原项目路径。
- 持有执行后端 Protocol 和契约测试。首版装配不可用实现，命令能力明确返回 `backend_unavailable`。

### WorkspaceBroker

Rust 所有的能力边界，提供逻辑接口：

- `list_entries(grant, relative_path)`
- `search_text(grant, relative_path, query, limits)`
- `read_file(grant, relative_path, limits)`
- `apply_changeset(approval, changeset)`
- `inspect_commit(commit_id)`

Broker 每次检查授权身份、Run ID、项目 ID、操作类别、相对路径和资源上限。它拒绝绝对路径、路径穿越、越出项目根目录的 symlink/junction/reparse point 及无授权请求。Python 只接收结构化结果，不接收超出已授权范围的目录句柄。

Python 与 Rust 使用仅本机宿主可访问的私有认证 RPC。协议不能通过 WebView 代理；传输选型、实例绑定、密钥生命周期与防重放要求在实施计划中细化，并复用桌面引擎已有的本机实例认证原则。

## 会话、消息与 Run

- Python 数据库保存 `sessions`、可见 `messages`、统一 `runs`、Run 事件、快照、待审批动作和迁移记录。
- `messages` 是桌面聊天历史的唯一权威源；Run 事件是操作与恢复事实，不用虚构历史 Run 重建旧操作时间线。
- Run 的 user input 持久化后才开始模型执行。assistant 最终回复使用 `(run_id, message_kind)` 唯一关联或等效幂等键，确保重投不重复显示。
- 阶段消息、能力调用、等待、批准/拒绝、ChangeSet 应用和终态以有序 Run 事件持久化；客户端用游标补齐断线期间事件。
- 运行状态未知时禁止猜测副作用已完成或安全重放。先核查操作账本和文件提交日志，再恢复、失败或进入人工检查。

## 项目理解与变更审批

1. 用户选择桌面项目，并针对当前 Run 显式批准读取范围。授权覆盖固定项目身份及列出、搜索、读取等只读操作。
2. Runtime 通过 WorkspaceBroker 按需读取项目内容。文件内容、README、代码注释和工具结果均为不可信数据，不能改变授权或策略。
3. Runtime 输出严格结构化 ChangeSet。每个条目包含操作类型（首版仅新增/修改）、项目相对路径、预期旧内容哈希（新增文件要求目标不存在）、新内容和摘要。首版拒绝删除、重命名、绝对路径及范围外路径。
4. Run 进入 `waiting`，桌面显示完整 diff。用户批准绑定不可变 ChangeSet 摘要、目标文件集合、项目 ID、Run ID 和授权版本的一次性审批票据；拒绝不产生写入。
5. Rust 应用前重新验证票据、路径和旧哈希。文件在审批后变化则报告冲突并保留提案，不覆盖外部修改。
6. Rust 先将内容写入同目录临时文件并 flush，再按文件记录和原子替换提交。多文件写入不是文件系统级整体原子操作，因此使用持久提交日志逐文件记录状态；崩溃后报告精确已应用集合并进入人工检查或显式恢复，不伪报整体成功。
7. 成功后追加 `workspace.change.applied` 事件，并通过幂等关联写入助手结果消息。UI 刷新后从消息和 Run 事件恢复相同结果。

## 执行后端契约

Runtime 只依赖版本化 `ExecutionBackend` Protocol。首版不暴露通用命令工具，但实现不可用后端并运行协议 conformance suite。后续执行器不得修改 Runtime 公开协议来表达平台特例；新增能力须版本化。

### 请求与生命周期

- `describe_capabilities() -> BackendCapabilities`：报告操作系统、命令执行、网络策略、工作区写入模型、资源限制和取消/核查能力。
- `prepare(run_id, workspace_snapshot, execution_policy) -> ExecutionHandle`：为一个 Run 创建隔离工作副本；接收不可变授权策略和工作区快照引用，不修改原项目。
- `execute(handle, invocation_id, argv, cwd, env_allowlist, timeout) -> ExecutionResult`：执行结构化 argv；默认拒绝 shell 字符串，避免命令拼接。网络、环境和资源由服务端 policy 决定，不由模型请求决定。
- `cancel(handle, invocation_id) -> CancelResult`、`inspect(handle, invocation_id) -> ExecutionObservation`：取消与核查精确操作身份；取消响应不自动等价于进程终止。
- `collect_changes(handle) -> ChangeSet`：导出工作副本差异，不直接写原项目。
- `close(handle) -> CleanupResult`：清理隔离副本与残留进程；结果未知时保留恢复记录。

### 状态与错误语义

Result status 至少包括 `completed`、`rejected`、`timed_out`、`cancelled`、`failed`、`unknown`。返回退出码、有限 stdout/stderr、截断标记和稳定错误码。执行结果未知、清理失败或无法核验时进入恢复检查，不自动重试，不回退到另一个权限更宽的后端。

Backend 按 Run 创建隔离工作区，不依赖容器保留 shell 状态。原项目不作为容器可写挂载目标；变更通过 ChangeSet 返回并复用相同审批/落盘路径。后端是否能联网必须由后端能力和 Run policy 显式决定；首版没有命令网络权限。

## 历史迁移与数据权威

- Rust 桌面库当前包含 sessions、messages、projects 和 desktop-to-engine session mappings；Python 引擎拥有 Agent sessions、Runs、事件和恢复状态。目标是迁移用户可见的会话/消息历史到 Python 数据库。
- 保留 session ID、message ID、创建时间、角色、正文、标题和 project association。桌面 Project 本身的目录位置、显示名和本机身份仍归 Rust 管理，Python Session 的 project_id 作为映射标识。
- 迁移按安装实例识别来源，采用可重入批次：复制批次与完成标记事务化或可幂等恢复；记录每批数目和稳定校验摘要。
- 切换读写前核对会话数、消息数、ID 集合、角色、时间、内容摘要、项目关联和会话内排序。任何核验失败则保留旧库为唯一活动源并显示迁移失败，不双写、不删旧库。
- 切换完成后 Python 是唯一读写源，Rust 库保持只读回退副本。首版不导入旧历史为 Run 或工具执行事件。

## 生命周期、故障处理与安全约束

- 应用启动先显示桌面 UI，不启动 Python。首次需要 Runtime 时由 Rust 按需启动引擎，完成版本/实例健康检查后复用进程。
- 模型密钥不进 SQLite、ChangeSet、Run 事件、WebView 状态或命令行参数；沿用现有私有启动凭据通道，向能力执行后端不传递 Provider 凭据。
- WorkspaceBroker 和执行后端均在服务端校验授权。前端可发起用户动作，不能单独授予权限。
- 文件写入有界限制路径数、单文件/总字节数、目录扫描深度、返回文本和事件 payload 大小。
- Run、审批票据、ChangeSet 和操作具有稳定 ID 与幂等约束。审批重放、旧 Run 动作、过期版本和内容哈希变化一律拒绝。
- 引擎/宿主断连时已持久化事实保留；未能证明完成的写操作标记 unknown，按提交日志核查后再决定恢复。
- 目录被删除、权限改变、磁盘不足、原子替换失败或用户取消均返回稳定可见错误。部分已提交文件明确列出，不称作已回滚。

## 分阶段实施与验收

### 阶段 A：统一会话与 Runtime 入口

- 删除桌面模式选择，所有输入进入统一 Python Runtime。
- 迁移会话/消息与项目关联，保留 ID，构建重入迁移和一致性校验；切换后单一读写源。
- 建立统一消息、Run 结果与事件游标 API；验证刷新/重启/重投恢复和结果去重。

验收：一条输入创建一个 Run；简单回答显示一次；会话与消息 ID 和顺序一致；迁移中断可重入；没有双写。

### 阶段 B：只读项目理解

- 落地私有 WorkspaceBroker 协议与 Run 级只读授权。
- 通过列目录、搜索和读文件理解所选项目，不引入 Shell。
- 验证授权范围、路径规范化、symlink/junction/reparse point 越界拒绝和资源上限。

验收：授权后可完成项目问答；未授权和越界请求拒绝；重启后 Run 状态和事件可恢复。

### 阶段 C：ChangeSet 审批与安全落盘

- Runtime 生成新增/修改 ChangeSet，桌面显示完整 diff。
- 实现一次性审批票据、拒绝零写入、审批后哈希冲突检测、逐文件原子写与持久提交日志。
- 验证中途崩溃、磁盘/权限异常、重复提交和部分多文件提交人工核查。

验收：拒绝时原项目字节不变；批准时只应用已批准 diff；内容冲突不覆盖；恢复界面准确报告部分状态。

### 阶段 D：执行后端协议冻结

- 固定版本化 ExecutionBackend Protocol、错误/取消/unknown 语义和契约测试。
- 首版提供 unavailable backend；没有 Docker/Windows sandbox 时命令能力明确不可用。
- 使用测试替身验证 prepare/execute/cancel/inspect/collect_changes/close 全部契约与故障情况。

验收：后端契约测试通过；Runtime 不依赖后端实现类型；不存在宿主机 fallback；未来后端仅需通过 conformance suite。

### 后续阶段（不属于本设计首版交付）

- 实现一个 Windows 原生沙箱后端或 Docker 后端并独立验收；比较进程隔离、文件访问、网络限制、进程树取消、未知副作用恢复、安装依赖与项目兼容性。
- 再把代码任务设计成动态 Skill / Workflow 能力；知识、写作仍各自保留领域服务和状态机。
- 在执行后端安全验收后，再讨论 Run 级联网审批、删除/重命名及其他副作用能力。

## 测试策略

- **Runtime/API**：消息→Run→工具事件→waiting→批准/拒绝→completed/failed；会话串行、请求幂等、取消、分页游标、进程重启和消息重投。
- **迁移**：ID、项目映射、排序、中文/大正文、重复批次、部分批次、迁移中进程退出与校验失败回退。
- **WorkspaceBroker**：授权缺失/过期/跨 Run、绝对路径和 traversal、大小/深度限制、symlink/junction 越界、目录消失及 ACL 错误。
- **ChangeSet 应用**：审批摘要篡改、旧哈希冲突、同路径多操作、临时文件清理、逐文件原子替换、进程崩溃、部分提交恢复、拒绝不写盘。
- **ExecutionBackend conformance**：参数拒绝、能力声明、超时、取消仍运行、inspect unknown、输出截断、重复 invocation、清理失败、变更集导出；首版 fake 实现不冒充真实隔离验收。
- **Windows 产品验收**：在 NTFS/实际 Windows 用户权限下验证路径与 reparse point、原子替换、权限失败、应用重启和隔离安装目录。Linux 单测不代替 Windows 文件系统验收。

## 风险与未覆盖项

- Rust/Python 双向能力桥增加本地认证与生命周期复杂度；实施前需具体化 IPC，确保仅同一受控桌面实例可用，WebView 无法访问。
- 多文件落盘不能跨任意目录提供通用 ACID 事务。持久日志能恢复并精确说明部分状态，但不应承诺每种外部占用/杀毒软件/磁盘故障下自动回滚成功。
- Runtime 生成内容可能不正确；审批 UI 必须展示完整可审查 diff，而摘要不能代替内容。
- 读取项目文件可能暴露密钥或个人数据；首版显式授权与路径范围限制不等同内容分类脱敏，读取策略和隐私提示需在 UI 专项确认。
- 执行后端 Protocol 的隔离级别由实现负责；Protocol 契约通过不表示 Docker、Windows 沙箱或系统级安全审计通过。
- 真实模型质量、Windows 安全隔离以及 Docker 镜像/依赖供应另行验收，不以 fake Provider、fake backend 或 Linux 测试推断通过。
