# 统一 Agent Runtime 讨论基线

> 用途：记录 2026-11-12 桌面端统一 Agent Runtime 讨论的目标、已确认边界和待研究问题，作为后续开源项目调研与架构讨论的起点。本文是讨论记录，不代表完整设计获批或实施已开始。

## 本轮目标

将 Tomato Agent 桌面端的普通聊天和复杂任务收敛到同一个 Python Agent Runtime，由 Runtime 统一接收会话输入、调度能力并管理执行过程；Rust 聚焦桌面窗口、本机集成、凭据与用户授权等桌面能力。Runtime 按需启动，并在桌面应用生命周期内复用引擎进程。

现有 Rust 聊天会话与消息历史需要迁入 Python 引擎并由引擎统一管理。桌面交互使用单一输入框，不再提供普通聊天、助手任务、写作等模式切换。会话均进入统一 Runtime；Runtime 可以发现和调度能力，但具体资源访问和副作用须经过显式授权。

## 已确认的能力边界

- 项目文件读取与固定目标测试不再作为僵硬的专用入口，方向是通过 CLI / 命令执行能力纳入 Runtime 调度。
- 知识检索与问答、写作作为业务能力暂时保持隔离，不在本轮强行改写或合并其领域流程。
- 代码任务视为可由动态 Skill / Workflow 配置接入的能力，暂时保持隔离。
- CLI 命令必须运行在沙箱中，不允许因沙箱不可用而静默回退到宿主机执行。
- Docker Desktop 可作为候选首版沙箱后端，但不应未经进一步讨论就变成 Runtime 或桌面应用的硬依赖。
- 项目目录写入需要明确授权。Linux 容器 UID 映射、Windows 挂载权限、原项目直接写入及中断后的无回滚风险仍是待解决问题。
- 将 Python、Node、Rust 工具链完整打进桌面安装包会带来明显体积和维护成本，镜像/工具链供应方式仍待调研。
- 任意会话的授权范围、授权有效期、网络权限、Run 与命令的生命周期、取消和未知副作用恢复需在完整方案中明确。

## 调研目标

只读研究 `W:\home\abeltomato\workspace\external\Agent` 下的开源项目，重点关注 OpenHands、Codex、Claude Code、Cline、LangGraph、OpenAI Agents、CAMEL 与 agents 等目录。比较其：

- Agent 主循环与统一调度模型，以及对普通对话和长任务的处理方式。
- 工具/能力注册、动态 Skill 或 Workflow 的边界。
- 用户授权如何表达、持久化、收窄与撤销。
- 本地命令执行、项目工作区和沙箱后端的实现及平台限制。
- Run / Session 事件、流式反馈、取消、崩溃恢复与未知副作用处理。
- 桌面宿主与执行 Runtime 的职责分界、进程生命周期和打包方式。

调研应区分各项目的源码事实、文档承诺和本地推断；引用文件路径与版本状态，记录设计取舍的适用范围，不把某项目的安全声明直接当作本项目验收证据。

## 本地调研快照

本轮只读检查的本地 Git 快照如下。它们是本机已有 checkout，不保证代表对应项目当前 upstream HEAD。

| 项目 | 本地提交 | 调研限制 |
| --- | --- | --- |
| Codex | `c0cc9fc` | 源码完整；Windows sandbox 实现规模较大，首轮只检查协议、策略、执行器和 app-server 说明 |
| OpenHands | `f691116` | 此 checkout 是 Agent Canvas 前端；其文档将 Agent Server / Runtime 标为独立仓库，本轮未检查该后端源码 |
| Claude Code | `927b664` | checkout 以用户文档、配置示例和插件为主，未发现产品 Runtime 源码；只据配置样例归纳权限表达 |
| Cline | `ea16751d6` | 本地工作树有未提交修改；保持只读，核心 SDK 架构与 Agent Runtime 源码可检查 |
| LangGraph | `799c2f0` | 框架源码可检查；重点检查 interrupt、checkpoint 与 resume 抽象 |
| OpenAI Agents Python | `125efa0` | SDK 源码可检查；需区分 SDK 的 Run / Session API 与带 Worker、业务仓储的应用 Runtime |
| CAMEL | `24fde60` | 工具包与多种 Runtime 后端可检查；文档的“sandbox”描述须和实际 local / Docker 行为区分 |
| Cloudflare Agents | `48d9c36` | Workers 专用实验性 Shell / Codemode 可检查；依赖 Cloudflare isolate 和 Durable Objects，不能直接作为本机方案 |

## 初步观察

- **Runtime 与执行隔离通常是两个边界。** Codex 的 `SandboxPolicy` 将文件系统、网络与执行策略显式建模，并把策略交给平台执行后端；Windows 有独立 MXC / 传统 sandbox 路径与配套测试。参考 `codex/codex-rs/protocol/src/protocol.rs`、`codex/codex-rs/exec-server/src/fs_sandbox.rs`、`codex/codex-rs/app-server/README.md`。
- **Agent loop 与持久化会话可以独立演进。** Cline 架构明确由 `@cline/agents` 持有无状态循环与工具调度，`@cline/core` 持有 Runtime 组合、Session 生命周期和持久化。参考 `cline/sdk/ARCHITECTURE.md`、`cline/sdk/packages/agents/src/agent-runtime.ts`。
- **授权是执行前的服务端/宿主门禁，不是模型决策。** Cline 的工具策略先经过 hook 和 policy，再通过宿主 callback 请求批准；未配置审批 callback 时拒绝执行。OpenAI Agents 的 approvals 作为 Run 中断项处理。Claude Code 的样例也分别配置 sandbox 命令限制与 sandbox 之外的工具权限，并明确 denyWrite 不控制 Agent 自己的 Edit 工具。参考 `cline/sdk/packages/agents/src/agent-runtime.ts`、`openai-agents-python/src/agents/run_internal/approvals.py`、`claude-code/examples/settings/settings-bash-sandbox.json`、`claude-code/examples/settings/README-hipaa.md`。
- **可恢复 Runtime 要持久化等待点和执行事实。** LangGraph 的 `interrupt` 将人工请求作为可恢复图中断；OpenAI Agents 将对话 Session 存储与 Run approval item 区分。Cloudflare Codemode 通过 durable log 记录 connector 调用状态，并在恢复时 replay 已记录结果，处理执行前崩溃窗口。参考 `langgraph/libs/prebuilt/langgraph/prebuilt/interrupt.py`、`openai-agents-python/src/agents/memory/sqlite_session.py`、`agents/packages/codemode/src/runtime.ts`。
- **“CLI 沙箱”不必等于“容器里跑任意 Shell”。** Cloudflare 的实验性 `@cloudflare/shell` 在 isolate 内执行 JavaScript，通过类型化 StateBackend 做文件操作，不解析 Bash；它带来结构化权限和可控文件语义，但无法直接替代本地 Python/Node/Rust 命令环境。参考 `agents/packages/shell/README.md`。
- **产品 Runtime 的工作区/Session 生命周期比单次工具调用更关键。** OpenHands 前端通过 Agent Server WebSocket 消费追加事件，支持历史和活动状态同步；但当前 checkout 不包含服务端执行器，不能用它判断 sandbox 后端设计。参考 `OpenHands/src/contexts/conversation-websocket-context.tsx`。
- **文档声明不能代替隔离证据。** CAMEL TerminalToolkit 有 local subprocess 与 Docker backend 两条路径，授权 callback 本身不限制获批程序的内部行为，也不替代 sandbox；其文档把 working directory 也称为本地安全边界，不能据此将路径限制等同操作系统隔离。参考 `camel/camel/toolkits/terminal_toolkit/terminal_toolkit.py`、`camel/docs/key_modules/terminaltoolkit.md`。

这些观察用于指导下一轮方案比较，不代表已选择某个项目实现或批准直接复用其代码。

## 本轮产物与下一步

本轮讨论已确定路线 1 为主：先交付统一 Runtime、项目只读理解、ChangeSet 提案、弹窗审批与安全落盘；通用 CLI 暂不作为首版能力。执行后端协议作为独立版本化接口固定，后续可实现 Docker 或 Windows 原生沙箱。完整组件、迁移、权限、恢复、接口和验收设计见[桌面端统一 Agent Runtime 设计](../superpowers/specs/2026-11-12-unified-desktop-agent-runtime-design.md)。

当前阶段为设计书面评审；设计通过后再制定实施计划。实现尚未开始。
