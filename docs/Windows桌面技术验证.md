# Windows 桌面技术验证

## 当前结论

2026-10-18 在 Windows 宿主与 Ubuntu WSL2 完成任务1的只读环境预检，状态为 **BLOCKED：尚未发现可用的 Windows Rust/Cargo 工具链**。Tauri 2 仍是候选框架，未创建桌面工程，未完成 Windows 构建、窗口启动、认证连接或原生凭据桥验收。

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

1. 人工提供已有 Windows Rust 的绝对位置，或单独授权安装 Windows Rust stable MSVC 工具链；预期 host 为 `x86_64-pc-windows-msvc`。本次未安装工具、改 PATH、提权或修改系统配置。
2. 在 Windows 新终端只读核验 `rustc -Vv`、`cargo --version`、`rustup show active-toolchain`，对齐项目要求的 pnpm `10.34.5`。MSVC/SDK 最终以实际 Windows 编译为准。
3. 恢复任务1：在仓库声明并锁定 Tauri 2 依赖，复用前端产物，建立固定 argv 的 WSL 发现/启动与身份验证接口，以及受控凭据通道。
4. 添加并执行原生单元测试，覆盖空格/非 ASCII 路径、恶意发行版字符串、未知协议、启动失败、实例端口冲突、令牌缺失/错误和日志脱敏。
5. Windows 实机验证窗口、所选 WSL 服务、健康与身份请求、目录及凭据桥，再生成并启动可分发构建。记录版本、完整命令、架构与产物 SHA-256。通过前不推进任务2，不调用真实模型。

本次没有代码改动，验证范围为环境命令输出、官方要求及文档一致性，不运行单元测试。尚无可分发产物，因此没有产物指纹。