# Windows 桌面工程

## 普通聊天 MVP（独立入口）

普通聊天版使用 `desktop-chat` Cargo feature 和 `tauri.chat.conf.json`，与下方既有 WSL 桌面链分开构建。开发启动：在仓库根目录运行 `cd desktop && pnpm dev:chat`；chat-only 构建使用 `pnpm build:chat`。聊天数据位于系统应用数据目录下的 `TomatoAgent/desktop-chat/chat.sqlite3`，模型端点/名称保存在同目录 `model.json`，API Key 只通过 Windows Credential Manager 保存。

Windows 用户配置、数据备份、网络错误和卸载说明见[Windows 普通聊天使用说明](../docs/Windows普通聊天使用说明.md)。

聊天版支持多会话创建、切换、重命名、确认删除，当前会话选择与历史恢复、历史分页接口，以及类似 Codex Projects 的本地项目归类：项目绑定一个本地目录，会话可移入或移出项目，删除项目不会删除会话或磁盘目录。OpenAI Chat Completions 兼容 SSE 流式回复、取消和显式连接测试也已支持。请求仅携带所选会话消息，不自动重试；失败或取消时不把部分回复保存成完整 assistant 消息。聊天运行时在 Tauri/Rust 进程内运行，不依赖 Python sidecar 或 WSL；SQLite 使用 bundled 库，API Key 由 Windows Credential Manager 保存。项目目录当前只用于会话整理，不授予文件访问权限。阶段 2 的工程测试和 Linux chat-mode 构建不等于阶段 3 的 Windows x64 安装资源启动验收，也不等于真实 Provider 验收；这些证据按 MVP 计划分别记录。

本工程已完成 Windows 桌面连接/生命周期的既有实现；桌面项目多目录计划阶段二的原生目录选择与路径桥接已实现，Windows 原生窗口人工验收待完成。共享前端在 Tauri 中提供 WSL 连接、原生认证请求桥、会话界面和受控退出；Web 模式保留原入口。既有 WSL 工作区的项目导航、自动标题及完整产品交付尚未完成；上方 chat-only 项目归类有独立实现与验收。连接、凭据、进程所有权及验收限制见 [桌面连接与生命周期](../docs/Windows桌面连接与生命周期.md)。

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

`scripts/windows.ps1` 封装相同命令，并在从 WSL 启动时补齐缺失的 Windows 可执行扩展名识别变量；若系统拒绝 UNC 未签名脚本，执行上面的明确命令，不更改执行策略。

普通聊天版 Windows Release/NSIS 使用整合脚本，从仓库根目录执行：

```powershell
& .\desktop\scripts\windows.ps1 -Action build-chat
```

脚本在当前 WSL 发行版中使用缓存的 pnpm 10.34.5 构建 `frontend/dist`，确认产物更新后，再初始化 Windows x64 MSVC 环境并调用 Windows 本地 Tauri CLI 生成 chat-only Release/NSIS。若工作树不在 `\\wsl.localhost\<发行版>\...` 路径下，可显式传入 `-WslDistro <发行版名>`。构建成功后，脚本自动把安装包及 `manifest.json` 归档到 `%LOCALAPPDATA%\TomatoAgent\installers\yyyyMMdd-HHmmss\`；原始构建产物保留，目标目录冲突时拒绝覆盖。归档字段与目录约定见 [Windows 安装包归档约定](../docs/local/windows-installer-archive.md)。

官方 NSIS 插件下载受阻时，手动下载后应核对 CLI 对应版本的官方哈希，缓存路径为 `%LOCALAPPDATA%\tauri\NSIS\Plugins\x86-unicode\additional\nsis_tauri_utils.dll`，不可省略 `additional`。CLI 2.12.3使用插件0.5.3，官方源码SHA-1为 `75197FEE3C6A814FE035788D1C34EAD39349B860`；本次实际文件SHA-256及安装包指纹见技术验证文档。

## 验证边界

桌面项目多目录阶段二：设置页的“选择并检查 Windows 文件夹”使用 Tauri 原生目录选择器。选择路径后，原生层只接受本地盘符路径，调用固定 argv 的 `wsl.exe ... wslpath -u <路径>`，要求返回值与 `/mnt/<drive>/...` 映射一致，并通过固定 `test -d` 检查目标发行版中的可访问性。UNC/网络路径和不支持路径明确拒绝。该操作只检查候选路径，不自动授予项目或会话文件访问权限。WSL 与 Windows 单元测试不能替代实际桌面窗口人工验收。

Windows 人工验收时：启动本机 release 应用，进入“设置”，选择并保存正在运行的 WSL 发行版；如需打开工作区，后端路径必须指向仓库的 `backend` 子目录（例如 `/home/<user>/.../Tomato-Agent/backend`），不是仓库根目录。点击“选择并检查 Windows 文件夹”不要求先打开工作区；依次验证含空格目录、含 Unicode 目录和取消选择。前两项应显示对应 `/mnt/<drive>/...` 路径，取消应显示取消提示；另选一个 WSL 不可访问/不存在的本地路径时应明确报错。确认选择操作没有创建项目、附加会话目录或启动文件任务。UNC 路径拒绝行为已有原生单元测试，Windows 文件夹选择器本身由操作系统对话框提供。

以下探针为任务1历史技术验证入口；日常窗口使用固定 `app.desktop.bootstrap` 连接业务服务，而非120秒探针。任务2可执行后端目标测试（工作目录`backend`）：`./.venv/bin/python -m pytest tests/test_desktop_connection.py tests/test_desktop_lifecycle.py tests/test_desktop_bootstrap.py tests/test_application.py tests/test_application_lifecycle.py tests/test_unified_tasks_api.py -q`。前端工作目录`frontend`：`node --test tests/desktop-connection.test.mjs tests/desktop-probe.test.mjs tests/unified-task-api.test.mjs tests/conversation-shell.test.mjs`，以及`pnpm build`。

WSL 启动固定为独立 argv：`wsl.exe --distribution <已发现发行版> --cd <后端绝对路径> --exec <后端>/.venv/bin/python -m app.desktop.probe`，不经 shell。Python 路径必须存在才能启动，启动错误使用固定摘要，不输出原始子进程日志。

令牌在 Windows 原生层生成，经 Windows 凭据管理器存取并删除；通过 stdin 传给 WSL，不进入前端、argv、URL 或日志。探针使用系统分配的 loopback 端口，所有路由检查令牌和实例。健康/身份还须匹配协议和实例，其他服务占据旧端口不能被当作成功连接。

目录桥只列出后端工作目录中的直接子目录并排除符号链接，不实现项目选择或任意路径读取。探针不初始化业务数据库、不启动 Worker、不调用 Provider，最多120秒后自行结束。错误或窗口关闭时不得用 `wsl --shutdown` 清场。

原生程序可通过固定验收入口生成脱敏报告：

```powershell
& "$env:CARGO_TARGET_DIR\release\tomato-agent-desktop.exe" --probe-report Ubuntu '<WSL后端绝对路径>' '<报告绝对路径>'
```

该入口调用与 UI 相同的原生连接逻辑，但不证明 WebView 界面交互成功；窗口与可分发产物仍需 Windows 实机证据。当前证据以 [Windows 桌面技术验证](../docs/Windows桌面技术验证.md) 为准。
