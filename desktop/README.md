# Windows 桌面技术验证工程

本工程仅用于 Windows 桌面实施计划任务1。共享前端在 Tauri 中展示技术验证界面，Web 模式继续展示会话界面。尚不提供完整助手服务生命周期或项目操作。

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

## 验证边界

WSL 启动固定为独立 argv：`wsl.exe --distribution <已发现发行版> --cd <后端绝对路径> --exec <后端>/.venv/bin/python -m app.desktop.probe`，不经 shell。Python 路径必须存在才能启动，启动错误使用固定摘要，不输出原始子进程日志。

令牌在 Windows 原生层生成，经 Windows 凭据管理器存取并删除；通过 stdin 传给 WSL，不进入前端、argv、URL 或日志。探针使用系统分配的 loopback 端口，所有路由检查令牌和实例。健康/身份还须匹配协议和实例，其他服务占据旧端口不能被当作成功连接。

目录桥只列出后端工作目录中的直接子目录并排除符号链接，不实现项目选择或任意路径读取。探针不初始化业务数据库、不启动 Worker、不调用 Provider，最多120秒后自行结束。错误或窗口关闭时不得用 `wsl --shutdown` 清场。

原生程序可通过固定验收入口生成脱敏报告：

```powershell
& "$env:CARGO_TARGET_DIR\release\tomato-agent-desktop.exe" --probe-report Ubuntu '<WSL后端绝对路径>' '<报告绝对路径>'
```

该入口调用与 UI 相同的原生连接逻辑，但不证明 WebView 界面交互成功；窗口与可分发产物仍需 Windows 实机证据。当前证据以 [Windows 桌面技术验证](../docs/Windows桌面技术验证.md) 为准。