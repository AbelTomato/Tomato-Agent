# 桌面应用

维护中的产品是 Tauri 桌面应用。React 界面位于 `ui/`；Rust 负责聊天存储、项目、凭据和随包分发的 Python 引擎生命周期。普通聊天无需启动引擎，复杂助手任务按需启动。

## 开发

首次使用时安装 Tauri CLI 和 UI 依赖：

```powershell
cd desktop
pnpm install
cd ui
pnpm install
cd ..
.\scripts\build-engine.ps1
pnpm dev
```

Rust 测试使用 `pnpm test:native`。UI 检查在 `ui` 目录运行 `pnpm build` 和 `node --test tests/*.test.mjs`。Python 引擎测试和打包依赖仍位于仓库 `backend/`，其中本地虚拟环境和数据目录保持原位。

## Windows 发布构建

从仓库根目录的 PowerShell 运行。`-Action test` 会先构建 Tauri 要求的真实 Python 引擎：

```powershell
.\desktop\scripts\windows.ps1 -Action test
.\desktop\scripts\windows.ps1 -Action build-release
```

单独开发引擎时运行 `desktop/scripts/build-engine.ps1`。`build-release` 会构建 UI、冻结 Windows Python 引擎、准备 x64 MSVC 环境、生成 Release 和 NSIS 安装包，并归档安装包及清单。构建脚本可从 WSL 检出目录读取源码；安装后的应用不会调用 WSL。
Windows 主机需要安装 Python 3.12 和 `uv` 并将其加入 `PATH`；引擎 runtime 与 PyInstaller 使用仓库 `backend/uv.lock` 中的锁定依赖。

引擎安装资源位于应用安装目录。用户数据、SQLite 聊天记录和偏好保存在安装目录之外。模型 Key 存入 Windows Credential Manager，不写入普通配置文件。
