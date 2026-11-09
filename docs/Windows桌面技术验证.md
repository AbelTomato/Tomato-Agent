# Windows 桌面技术验证

> 历史记录：本文只记录早期 WSL 桌面技术选型和验收，不代表当前产品路径。当前实现见[桌面工程说明](../desktop/README.md)。

## 当前结论

2026-10-18 21:43，任务1状态为 **DONE：Windows NSIS 构建、安装、已安装窗口及 WSL 认证/目录/凭据桥验证通过**。Windows 原生测试补至9/9，通过真实loopback端口冲突与凭据脱敏用例。后端探针4/4、桌面UI桥1/1、现有会话17/17及前端构建复用此前未受本次测试/文档修改影响的通过证据。Tauri 2 可作为后续切片技术选型；本次仅完成任务1，不推进任务2，不代表完整桌面助手已交付。以下环境表与早期阻塞段落保留历史事实，最终结果见末节。

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

## 历史阻塞与恢复过程

原 Rust 缺项已解除：从 Rust 官方分发地址获取安装器，固定官方发布 `1.99.1-x86_64-pc-windows-msvc`；未改系统 PATH。Windows 原生下载 rustc 卡住后，使用 WSL 从同一官方地址下载，SHA-256 与官方 channel manifest 完全一致，再复用 rustup 下载缓存。首次安装器文件名错误已修正；恢复 partially installed stable 时额外请求组件，改为固定版本 minimal 安装成功。Windows pnpm 通过 `npm exec --package=pnpm@10.34.5` 使用，不替换全局版本。Tauri CLI `2.12.3` 已在仓库声明并锁定，Windows 使用本地缓存副本。

工程入口与实际 Windows 命令见 [桌面工程说明](../desktop/README.md)。首次原生编译缺少 icon.ico，补充原始32×32图标后通过；UNC 未签名 PowerShell 脚本被系统策略拒绝，实际验证使用明确命令，未更改执行策略。

1. 人工提供已有 Windows Rust 的绝对位置，或单独授权安装 Windows Rust stable MSVC 工具链；预期 host 为 `x86_64-pc-windows-msvc`。本次未安装工具、改 PATH、提权或修改系统配置。
2. 在 Windows 新终端只读核验 `rustc -Vv`、`cargo --version`、`rustup show active-toolchain`，对齐项目要求的 pnpm `10.34.5`。MSVC/SDK 最终以实际 Windows 编译为准。
3. 恢复任务1：在仓库声明并锁定 Tauri 2 依赖，复用前端产物，建立固定 argv 的 WSL 发现/启动与身份验证接口，以及受控凭据通道。
4. 添加并执行原生单元测试，覆盖空格/非 ASCII 路径、恶意发行版字符串、未知协议、启动失败、实例端口冲突、令牌缺失/错误和日志脱敏。
5. Windows 实机验证窗口、所选 WSL 服务、健康与身份请求、目录及凭据桥，再生成并启动可分发构建。记录版本、完整命令、架构与产物 SHA-256。通过前不推进任务2，不调用真实模型。

首次预检没有代码改动。恢复后增加独立探针、原生桥与桌面验证 UI；探针不接入业务 API、不初始化业务库，不代表切片2生命周期实现已完成。产物与实机验证结果以后续记录为准。

## 21:36 Windows 实机结果与当时阻塞（历史）

- 执行 Windows `cargo test --manifest-path <仓库>\desktop\src-tauri\Cargo.toml --locked`，7/7 通过；构建目标 `x86_64-pc-windows-msvc`，Cargo/rustc `1.99.1`。MSVC/SDK 由 Cargo 自动发现；安装版本见环境表，具体编译器选择未单独记录，不据此宣称两个版本均验证。
- 在 `desktop` 执行 Windows `node.exe <Windows CLI缓存>\tauri.js build -- --locked`，release 编译成功（2分44秒）；随后 NSIS 插件下载失败，整条打包命令失败，不能称安装包构建通过。
- 原生 `--probe-report` 实际访问 Ubuntu WSL：health/identity 的协议与实例一致，目录根正确，Windows 凭据管理器往返成功，缺失/错误令牌返回401，错误实例返回409。外层调用等待超过工具30秒，但脱敏报告已写入；原因是 WSL 启动的探针继续存活至120秒边界，之后核验进程已退出。此行为是技术探针限制，后续生命周期切片必须独立实现。
- 实际启动 release exe，通过 Windows UI Automation RawView 读取技术验证页面，并点击发行版发现、填入已有 WSL 后端路径、点击验证；页面展示完整结果及四个 true 标志。无需 Vite dev server。此为自动化实机观察，未冒充人工截图验收。
- 产物：`C:\Users\AbelTomato\AppData\Local\TomatoAgent\task1-target\release\tomato-agent-desktop.exe`，9,963,008 字节，SHA-256 `7cc4763e494966e2e15b2e69ce67bcb03e94e810545d72be7ca6d115918d9d79`。这是已启动的 release exe；尚无 NSIS 安装包及安装验证。
- 本地工件位于 `backend/data/desktop-acceptance/task1-20261018/`：`windows-native-tests-3.log`、`windows-release-build.log`、`native-connection.json`、`window-raw.txt`、`ui-result.txt` 及依赖安装日志。该目录保持 Git 忽略；没有真实 Provider 请求。

NSIS `3.13` 官方工具包已下载、校验并解压，但 `https://github.com/tauri-apps/nsis-tauri-utils/releases/download/nsis_tauri_utils-v0.5.3/nsis_tauri_utils.dll` 在 Windows 打包器中 `timeout: global`；WSL GET 20秒无数据，定向 IPv4 检查连接8秒超时。曾成功的 HEAD 仅证明当时可取响应头，不证明文件下载可用。不切换非官方镜像或跳过校验。

解除条件：人工恢复上述官方资源下载可达性，或提供来源和完整性可核验的官方文件；随后重跑桌面构建命令，生成安装包、记录指纹并在 Windows 启动产物后才能关闭任务1。现有单元测试未真实绑定一个冲突端口，只覆盖实例响应拒绝逻辑；如需严格关闭该验收项，还须补充原生本机冲突响应集成用例。当前为明确未完成项，不扩大已有测试结论。

## 21:43 最终验收

用户手动下载官方插件后，本地 SHA-256 为 `5ba143b5db4a87d32d6e7802e033330aae56cbceabe0d1e3ba41948385ad4709`。对照 [Tauri CLI 2.12.3 官方打包器源码](https://raw.githubusercontent.com/tauri-apps/tauri/tauri-cli-v2.12.3/crates/tauri-bundler/src/bundle/windows/nsis/mod.rs)，SHA-1 `75197fee3c6a814fe035788d1c34ead39349b860` 完全一致。此前提供给用户的缓存路径漏了 `additional`，导致打包器判定缺文件并重建缓存；从用户保留的临时下载恢复至 `%LOCALAPPDATA%\tauri\NSIS\Plugins\x86-unicode\additional\nsis_tauri_utils.dll` 后通过检查，无跳过校验。

- Windows原生测试9/9：新增真正绑定loopback端口并返回错误实例的HTTP集成用例，以及Authorization敏感标记/Debug脱敏检查；此前路径、恶意发行版、协议与启动失败测试继续通过。日志 `windows-native-final.log`，`TEST_EXIT_CODE: 0`。
- Windows CLI在desktop目录执行 `node.exe <Windows CLI缓存>\tauri.js build -- --locked`，release与NSIS全部完成，`windows-package-final.log` 记录 `BUILD_EXIT_CODE: 0`。
- 安装器通过 `Start-Process -FilePath <setup> -ArgumentList @('/S', '/D=<用户验收目录>') -PassThru -Wait` 安装到 `C:\Users\AbelTomato\AppData\Local\TomatoAgent\task1-installed`，退出码0，无提权或系统保护绕过。安装记录见 `install-final.log`。
- 启动安装目录的exe，通过UI Automation点击发行版发现、输入WSL后端目录并执行验证；`installed-ui-result.txt` 显示协议1、正确根目录和实例、credential_roundtrip及三项拒绝检查均true。`installed-window.json` 和 `installed-window.png` 保存窗口证据。已正常关闭验收窗口，探针按120秒边界自行退出。

| 产物 | Windows绝对路径 | SHA-256 |
| --- | --- | --- |
| NSIS安装包（2,282,846字节） | `C:\Users\AbelTomato\AppData\Local\TomatoAgent\task1-target\release\bundle\nsis\Tomato Agent_0.1.0_x64-setup.exe` | `b464e591ac74d325a62e317997c6ef30655da7285a899e5507e0e4010c6b1809` |
| 安装后应用（实际启动） | `C:\Users\AbelTomato\AppData\Local\TomatoAgent\task1-installed\tomato-agent-desktop.exe` | `8d78daeb862b103cadfef9e45149c039c1fa479ea3b008ff58a9e56b444fe1c7` |

产物未签名，不承诺消除SmartScreen；安装包与打包缓存exe/安装后exe的指纹分别记录，不假设打包器补丁前后字节相同。版本、架构和环境见前文；MSVC自动发现，未宣称已分别验证所有已安装编译器版本。验收为Windows实机自动化，未要求用户完成额外人工观察；完整产品人工矩阵属于后续切片。此探针仅验证技术可行性，不初始化业务库或调用Provider，完整业务认证及生命周期仍由任务2负责。

## 2026-11-01 Windows 原生聊天版构建与安装验收

- 聊天版前端以 Vite `desktop-chat` mode 构建；Windows x64 Release 使用 `desktop-chat` 与 `tauri/custom-protocol` features 编译。因 Windows 子进程不能从 WSL UNC 工作目录运行前端命令，先在 WSL 生成 `frontend/dist`，再运行 Windows Cargo 与 Tauri NSIS bundle。
- NSIS 安装器：`C:\Users\AbelTomato\AppData\Local\TomatoAgent\task1-target\release\bundle\nsis\Tomato Agent_0.1.0_x64-setup.exe`，2,713,237 字节，SHA-256 `b839b8dc17b3674c9ca0da2049f9292cadb6a7a9114cb1cadc289b767d18c4d7`。
- 配套 Release exe：`C:\Users\AbelTomato\AppData\Local\TomatoAgent\task1-target\release\tomato-agent-desktop.exe`，10,530,816 字节，SHA-256 `8139372ebba7a98495f24ea8508818475419f1378187d642cf956819cff8c6ef`。
- `frontend/tests/desktop-chat-entry.test.mjs` 3/3 通过；用户确认安装后验收通过。安装目录、实机截图及独立 UI 自动化记录未提供，本条不补记这些证据。该验收结论仅针对本聊天版安装包，不覆盖 Provider 真实请求验收。
