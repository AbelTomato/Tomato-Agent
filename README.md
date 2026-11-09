# Tomato Agent

Tomato Agent 是面向 Windows 的桌面助手，提供普通聊天、知识检索、技术写作和受控代码任务。维护中的产品入口是 Tauri 桌面应用，不再提供浏览器界面或独立 Web 部署。

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
pnpm dev
```

从 WSL 检出目录构建 Windows x64 Release/NSIS 安装包，可在仓库根目录的 PowerShell 中运行：

```powershell
.\desktop\scripts\windows.ps1 -Action build-release
```

脚本会构建界面、打包 Windows Python 引擎、生成 Tauri 安装包，并附带 SHA-256 清单归档。安装后的应用不依赖 WSL 或系统 Python。

首次配置、数据备份和故障处理见 [Windows 桌面使用说明](docs/Windows普通聊天使用说明.md)。

## 开发验证

```bash
cd desktop/ui && pnpm build && node --test tests/*.test.mjs
cargo test --manifest-path desktop/src-tauri/Cargo.toml --locked
backend/.venv/bin/python -m pytest backend/tests
```

常规 Python 测试默认使用 fake provider。Docker、OpenShell 和真实 Provider 验收是独立的显式 opt-in 工作流。

## 迁移记录

架构与语言职责说明见[桌面端架构迁移与语言选型建议](docs/桌面端架构迁移与语言选型建议.md)。`docs` 下的 Web/WSL 文件仅作为历史验收记录，不再描述受支持的产品入口。
