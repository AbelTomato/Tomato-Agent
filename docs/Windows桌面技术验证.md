# Windows 桌面技术验证

## 当前结论

2026-10-18 21:36，任务1状态为 **BLOCKED：Windows release exe、实机窗口与认证/目录/凭据桥已通过，NSIS 安装包因官方插件下载超时未生成**。用户批准安装依赖后已补齐 Windows Rust/Cargo `1.99.1` MSVC；Windows 原生测试 7/7、后端探针测试 4/4、桌面 UI 桥测试 1/1、现有会话测试 17/17 通过，共享前端构建通过。Tauri 2 技术链路已取得实际证据，但完整切片门禁尚未通过，不推进任务2。下表保留首次只读预检事实。

按 [Tauri 2 官方前置要求](https://v2.tauri.app/start/prerequisites/)，Windows 开发需要 MSVC C++ Build Tools、WebView2 和 Rust MSVC 工具链。Linux Rust 不能替代本任务的 Windows 实机门禁。

## 环境证据

| 检查项 | 实际观察 | 结论 |
| --- | --- | --- |
| Windows | x64，WSL 版本输出 Windows `10.0.26200.9711` | 可调用宿主 PowerShell |
| Windows Rust/Cargo | `Get-Command rustc.exe,cargo.exe` 未找到；`C:\Users\AbelTomato\.cargo\bin` 下 rustup/rustc/cargo 均不存在；未配置额外 CARGO_HOME | 阻塞；不能排除安装在未声明的自定义位置 |
| MSVC | vswhere 找到 `D:\Dev-Tools\MSVC\tool`，Build Tools `18.7.11925.98`；x64 cl 文件版本 `19.44.35228.0`、`19.51.36248.0` | 已发现 C++ 组件，未实际编译；普通 PATH 无 cl 不等于未安装 |
| Windows SDK | Include 目录 `10.0.26100.0`、`10.0.28000.0` | 已发现，未实际链接 |
| WebView2 | 注册表记录 `155.0.4283.62` | 已发现，未启动嵌入式窗口 |
| Windows Node/pnpm | Node `24.18.0`，pnpm `11.3.0` | pnpm 与仓库要求 `10.34.5` 不一致，恢复时需对齐 |
| WSL | `2.7.8.0`；Ubuntu Running / WSL2，docker-desktop Stopped / WSL2 | 目标发行版可见，未启动其他发行版 |
| WSL 网络 | `wslinfo --networking-mode` 返回 `nat` | 只确认模式；未证明 Windows 到后端可达 |
| WSL Node/pnpm | Node `22.22.1`，pnpm `10.34.5` | 当前 Web 开发工具存在 |
| 后端 | `backend/.venv/bin/python --version` 返回 `3.14.4`；`backend/app/main.py` 存在 | 仅检查解释器与入口，未启动服务或访问业务库 |

PowerShell 通过 `-NoProfile -NonInteractive` 执行检查，MSVC 通过 vswhere 与文件版本确认，WebView2 通过 EdgeUpdate Clients 注册表确认。Windows pnpm 从 WSL UNC 工作目录调用时出现 CMD 不支持 UNC 工作目录的提示；后续 Windows 构建需选择并记录可用工作目录，当前不据此认定构建可行。

## 解除阻塞与恢复

原 Rust 缺项已解除：从 Rust 官方分发地址获取安装器，固定官方发布 `1.99.1-x86_64-pc-windows-msvc`；未改系统 PATH。Windows 原生下载 rustc 卡住后，使用 WSL 从同一官方地址下载，SHA-256 与官方 channel manifest 完全一致，再复用 rustup 下载缓存。首次安装器文件名错误已修正；恢复 partially installed stable 时额外请求组件，改为固定版本 minimal 安装成功。Windows pnpm 通过 `npm exec --package=pnpm@10.34.5` 使用，不替换全局版本。Tauri CLI `2.12.3` 已在仓库声明并锁定，Windows 使用本地缓存副本。

工程入口与实际 Windows 命令见 [桌面工程说明](../desktop/README.md)。首次原生编译缺少 icon.ico，补充原始32×32图标后通过；UNC 未签名 PowerShell 脚本被系统策略拒绝，实际验证使用明确命令，未更改执行策略。

1. 人工提供已有 Windows Rust 的绝对位置，或单独授权安装 Windows Rust stable MSVC 工具链；预期 host 为 `x86_64-pc-windows-msvc`。本次未安装工具、改 PATH、提权或修改系统配置。
2. 在 Windows 新终端只读核验 `rustc -Vv`、`cargo --version`、`rustup show active-toolchain`，对齐项目要求的 pnpm `10.34.5`。MSVC/SDK 最终以实际 Windows 编译为准。
3. 恢复任务1：在仓库声明并锁定 Tauri 2 依赖，复用前端产物，建立固定 argv 的 WSL 发现/启动与身份验证接口，以及受控凭据通道。
4. 添加并执行原生单元测试，覆盖空格/非 ASCII 路径、恶意发行版字符串、未知协议、启动失败、实例端口冲突、令牌缺失/错误和日志脱敏。
5. Windows 实机验证窗口、所选 WSL 服务、健康与身份请求、目录及凭据桥，再生成并启动可分发构建。记录版本、完整命令、架构与产物 SHA-256。通过前不推进任务2，不调用真实模型。

首次预检没有代码改动。恢复后增加独立探针、原生桥与桌面验证 UI；探针不接入业务 API、不初始化业务库，不代表切片2生命周期实现已完成。产物与实机验证结果以后续记录为准。

## Windows 实机结果与剩余阻塞

- 执行 Windows `cargo test --manifest-path <仓库>\desktop\src-tauri\Cargo.toml --locked`，7/7 通过；构建目标 `x86_64-pc-windows-msvc`，Cargo/rustc `1.99.1`。MSVC/SDK 由 Cargo 自动发现；安装版本见环境表，具体编译器选择未单独记录，不据此宣称两个版本均验证。
- 在 `desktop` 执行 Windows `node.exe <Windows CLI缓存>\tauri.js build -- --locked`，release 编译成功（2分44秒）；随后 NSIS 插件下载失败，整条打包命令失败，不能称安装包构建通过。
- 原生 `--probe-report` 实际访问 Ubuntu WSL：health/identity 的协议与实例一致，目录根正确，Windows 凭据管理器往返成功，缺失/错误令牌返回401，错误实例返回409。外层调用等待超过工具30秒，但脱敏报告已写入；原因是 WSL 启动的探针继续存活至120秒边界，之后核验进程已退出。此行为是技术探针限制，后续生命周期切片必须独立实现。
- 实际启动 release exe，通过 Windows UI Automation RawView 读取技术验证页面，并点击发行版发现、填入已有 WSL 后端路径、点击验证；页面展示完整结果及四个 true 标志。无需 Vite dev server。此为自动化实机观察，未冒充人工截图验收。
- 产物：`C:\Users\AbelTomato\AppData\Local\TomatoAgent\task1-target\release\tomato-agent-desktop.exe`，9,963,008 字节，SHA-256 `7cc4763e494966e2e15b2e69ce67bcb03e94e810545d72be7ca6d115918d9d79`。这是已启动的 release exe；尚无 NSIS 安装包及安装验证。
- 本地工件位于 `backend/data/desktop-acceptance/task1-20261018/`：`windows-native-tests-3.log`、`windows-release-build.log`、`native-connection.json`、`window-raw.txt`、`ui-result.txt` 及依赖安装日志。该目录保持 Git 忽略；没有真实 Provider 请求。

NSIS `3.13` 官方工具包已下载、校验并解压，但 `https://github.com/tauri-apps/nsis-tauri-utils/releases/download/nsis_tauri_utils-v0.5.3/nsis_tauri_utils.dll` 在 Windows 打包器中 `timeout: global`；WSL GET 20秒无数据，定向 IPv4 检查连接8秒超时。曾成功的 HEAD 仅证明当时可取响应头，不证明文件下载可用。不切换非官方镜像或跳过校验。

解除条件：人工恢复上述官方资源下载可达性，或提供来源和完整性可核验的官方文件；随后重跑桌面构建命令，生成安装包、记录指纹并在 Windows 启动产物后才能关闭任务1。现有单元测试未真实绑定一个冲突端口，只覆盖实例响应拒绝逻辑；如需严格关闭该验收项，还须补充原生本机冲突响应集成用例。当前为明确未完成项，不扩大已有测试结论。