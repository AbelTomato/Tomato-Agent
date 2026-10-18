# Windows 桌面工程

本工程已完成 Windows 桌面实施计划任务1、2。共享前端在 Tauri 中提供 WSL 连接、原生认证请求桥、会话界面和受控退出；Web 模式保留原入口。项目导航、自动标题及完整产品交付尚未完成。连接、凭据、进程所有权及验收限制见 [桌面连接与生命周期](../docs/Windows桌面连接与生命周期.md)。

## 已声明依赖与路径

- Windows x64 Rust `1.99.1` MSVC、C++ Build Tools、Windows SDK、WebView2。
- pnpm `10.34.5`，Tauri CLI `2.12.3`；Rust 实际依赖由 `src-tauri/Cargo.lock` 锁定。
- 前端产物固定为仓库 `frontend/dist`；先在 `frontend` 执行 `pnpm build`。
- Windows 构建缓存为 `%LOCALAPPDATA%\TomatoAgent\task1-target`，不加入 Git。
- Windows CLI 本地缓存为 `%LOCALAPPDATA%\TomatoAgent\task1-tools`；不要用 WSL 安装的原生 CLI 二进制执行 Windows 构建。

## Windows 命令

在 PowerShell 中进入本工程。将 Cargo 的用户 bin 目录加入当前进程 PATH，设置本地缓存：

```powershell
$env:PATH = (Join-Path $env:USERPROFILE '.cargo\bin') + ';' + $env:PATH
$env:CARGO_TARGET_DIR = Join-Path $env:LOCALAPPDATA 'TomatoAgent\task1-target'
cargo test --manifest-path .\src-tauri\Cargo.toml --locked
$cli = Join-Path $env:LOCALAPPDATA 'TomatoAgent\task1-tools\node_modules\@tauri-apps\cli\tauri.js'
node.exe $cli build -- --locked
```

首次准备 Windows CLI 缓存（版本与仓库 package.json 一致）：

```powershell
$tools = Join-Path $env:LOCALAPPDATA 'TomatoAgent\task1-tools'
New-Item -ItemType Directory -Force $tools | Out-Null
Push-Location $tools
npm.cmd exec --yes --package=pnpm@10.34.5 -- pnpm add --save-exact @tauri-apps/cli@2.12.3
Pop-Location
```

`scripts/windows.ps1` 封装相同命令；若系统拒绝 UNC 未签名脚本，执行上面的明确命令，不更改执行策略。

官方 NSIS 插件下载受阻时，手动下载后应核对 CLI 对应版本的官方哈希，缓存路径为 `%LOCALAPPDATA%\tauri\NSIS\Plugins\x86-unicode\additional\nsis_tauri_utils.dll`，不可省略 `additional`。CLI 2.12.3使用插件0.5.3，官方源码SHA-1为 `75197FEE3C6A814FE035788D1C34EAD39349B860`；本次实际文件SHA-256及安装包指纹见技术验证文档。

## 验证边界

以下探针为任务1历史技术验证入口；日常窗口使用固定 `app.desktop.bootstrap` 连接业务服务，而非120秒探针。任务2可执行后端目标测试（工作目录`backend`）：`./.venv/bin/python -m pytest tests/test_desktop_connection.py tests/test_desktop_lifecycle.py tests/test_desktop_bootstrap.py tests/test_application.py tests/test_application_lifecycle.py tests/test_unified_tasks_api.py -q`。前端工作目录`frontend`：`node --test tests/desktop-connection.test.mjs tests/desktop-probe.test.mjs tests/unified-task-api.test.mjs tests/conversation-shell.test.mjs`，以及`pnpm build`。

WSL 启动固定为独立 argv：`wsl.exe --distribution <已发现发行版> --cd <后端绝对路径> --exec <后端>/.venv/bin/python -m app.desktop.probe`，不经 shell。Python 路径必须存在才能启动，启动错误使用固定摘要，不输出原始子进程日志。

令牌在 Windows 原生层生成，经 Windows 凭据管理器存取并删除；通过 stdin 传给 WSL，不进入前端、argv、URL 或日志。探针使用系统分配的 loopback 端口，所有路由检查令牌和实例。健康/身份还须匹配协议和实例，其他服务占据旧端口不能被当作成功连接。

目录桥只列出后端工作目录中的直接子目录并排除符号链接，不实现项目选择或任意路径读取。探针不初始化业务数据库、不启动 Worker、不调用 Provider，最多120秒后自行结束。错误或窗口关闭时不得用 `wsl --shutdown` 清场。

原生程序可通过固定验收入口生成脱敏报告：

```powershell
& "$env:CARGO_TARGET_DIR\release\tomato-agent-desktop.exe" --probe-report Ubuntu '<WSL后端绝对路径>' '<报告绝对路径>'
```

该入口调用与 UI 相同的原生连接逻辑，但不证明 WebView 界面交互成功；窗口与可分发产物仍需 Windows 实机证据。当前证据以 [Windows 桌面技术验证](../docs/Windows桌面技术验证.md) 为准。