# Tomato Agent

Tomato Agent始于2026.09，最开始是因为碰到一家小厂的笔试题，要求实现一个最小可用Agent，当时刚好想要入手学习Agent这个领域的知识，比较感兴趣，所以就开始做了这个项目

这个项目主要是记录我一些学习Agent的探索，从最开始的基础的基于ReAct的Agent Loop，滑动窗口做Context截断，再到现在已经实现的RAG系统，基础的Observability、Eval，还有长期Memory系统，后来也可能继续探索Agent的隔离运行环境等等，总而言之学无止境也

> Updated 2026.11.12

## 架构

- `desktop/ui` 使用 React 和 TypeScript 实现桌面界面。
- Rust/Tauri 管理窗口、普通聊天、会话和项目存储、系统凭据及引擎进程。
- Python 保留既有助手、知识、写作、执行和恢复内核；FastAPI 仅作为由桌面应用管理的本机内部协议适配层。
- 普通聊天不启动 Python；复杂任务需要时再启动随安装包分发的引擎。

Rust 负责桌面系统集成与本机生命周期，Python 继续承载成熟的任务执行和恢复逻辑，TypeScript 负责界面。将 Agent 内核整体改写为 Rust 会复制现有状态机与领域能力，不是本次迁移的默认方向。

## 启动桌面应用

安装桌面工具和 UI 依赖：

```powershell
cd desktop
pnpm install
cd ui
pnpm install
cd ..
.\scripts\build-engine.ps1
pnpm dev
```

从 WSL 检出目录构建 Windows x64 Release/NSIS 安装包，可在仓库根目录的 PowerShell 中运行：

```powershell
.\desktop\scripts\windows.ps1 -Action build-release
```

脚本会构建界面、打包 Windows Python 引擎、生成 Tauri 安装包，并附带 SHA-256 清单归档。安装后的应用不依赖 WSL 或系统 Python。
Windows 开发和构建需要安装 Python 3.12 和 `uv` 并将 `uv` 加入 `PATH`；引擎构建依赖通过 `backend/uv.lock` 安装。

首次配置、数据备份和故障处理见 [Windows 桌面使用说明](docs/guides/Windows普通聊天使用说明.md)。

## 开发验证

```bash
cd desktop/ui && pnpm build && node --test tests/*.test.mjs
```

Windows PowerShell 中先构建 Tauri 所需的 Python 引擎，再运行 Rust 测试：

```powershell
.\desktop\scripts\build-engine.ps1
cargo test --manifest-path desktop/src-tauri/Cargo.toml --locked
```

```bash
backend/.venv/bin/python -m pytest backend/tests
```

常规 Python 测试默认使用 fake provider。Docker、OpenShell 和真实 Provider 验收是独立的显式 opt-in 工作流。

## 迁移记录

架构与语言职责说明见[桌面端架构迁移与语言选型建议](docs/architecture/桌面端架构迁移与语言选型建议.md)。文档分类与归档入口见 [文档索引](docs/README.md)。
