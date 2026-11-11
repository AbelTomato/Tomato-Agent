# Tomato Agent 首次 GitHub Release 设计

## 目标

将当前本地 `main` 上已有的桌面迁移提交与工作区改动按功能拆分成可审阅提交，推送至 `origin/main`，建立可重复的 GitHub Actions 验证与 Windows x64 Release 流程，并发布首个 `v0.1.0` GitHub Release。

## 当前事实

- 应用版本在 `desktop/src-tauri/Cargo.toml` 与 `desktop/src-tauri/tauri.conf.json` 中为 `0.1.0`。
- 当前 `main` 相对跟踪分支 `origin/main` 有 7 个本地提交；本次不改写这些提交。
- `.github/workflows/verification.yml` 已有 Ubuntu 后端/UI 验证，但没有 Windows 原生测试，也未显式运行新的分层记忆测试。
- Windows 发布产物为未签名的 x64 NSIS 安装器；仓库已有桌面打包流程和 PyInstaller 引擎构建参数，但该脚本依赖本机 WSL、MSVC 和工具缓存，GitHub hosted runner 需在 workflow 中直接复现其确定性步骤。
- 新分层记忆定向测试通过；当前完整 memory module gate 中有 2 条依赖旧自动提取器装配的测试失败，不能把这两项隐藏成绿色结果。

## 方案

### 提交组织

先按差异和依赖将未提交工作分为独立提交：桌面 chat 项目归类与会话治理；Python Agent/桌面引擎接入与分层记忆；记忆评测、fixture 和使用/架构文档；必要的验证脚本调整。旧有 7 个提交保持原样。仅提交 Git 当前可见的 tracked/untracked 源码和文档，不强制添加被忽略的本地开发日志、环境或构建工件。提交前逐组审查 staged diff，并确认没有凭据、数据库、模型输出或本机路径产物。

### CI

保留现有 Ubuntu Verification workflow，补充新 layered-memory 定向回归，并在 Windows runner 执行 Rust 全量 `cargo test --locked`。当前 `Cargo.toml` 没有 `desktop-chat` feature。CI 使用锁定的 Python、Node、pnpm 和 Cargo lockfiles，不使用真实模型密钥，不运行真实 Provider 请求、Docker/OpenShell 外部验收或 180 请求盲评。发布阻断以被 workflow 明确执行的绿色检查为准；旧 memory module 全量门禁在其装配契约另行处理前不纳入本次 CI 命令，但其两项失败须在发布记录中如实注明。

### Release

新增 Windows x64 Release workflow，支持 `workflow_dispatch` dry-run 与 `v0.1.0` tag 正式发布。Windows runner 构建前端、使用仓库现有收集参数打包 Python 引擎，再用 Tauri/Cargo lockfile 生成 NSIS 安装器。手动运行只上传 Actions artifact 供验证，不创建 Release；只有推送 `v0.1.0` tag 才能发布。workflow 生成安装器 SHA-256 和包含版本、commit、目标平台、文件大小与摘要的 manifest，先校验工件并创建 draft，再上传资产后发布；中断时保留 draft 供同一 tag 安全重跑。仅发布 job 获得 `contents: write`。发布说明明确该安装器未签名及现有文档列出的安装/卸载验收限制。

### 推送与发布顺序

先在本地完成差异审查、分组提交和与 CI 等价的本地验证，再确认 `origin/main` 没有新增提交；若远端前进则停止并重审，不强推。推送 main 后等待 GitHub CI 通过；其后创建并推送 `v0.1.0` tag，由 Release workflow 构建和发布。Release workflow 或构建失败时保留 main 提交，不删除 tag/Release 工件来掩盖失败，先修复并以同一版本 tag 的安全重跑流程解决。

## 验收

- 当前工作区的所有非忽略改动均已按功能形成独立、可读提交，提交树不含本地密钥、数据库和构建产物。
- `origin/main` 指向所有预期本地提交，远端没有被强制改写。
- Verification workflow 在 GitHub Actions 中通过，包含 Python Assistant/integration、新 layered-memory 测试、桌面 UI 构建/测试和 Windows Rust 测试。
- `v0.1.0` Release 发布成功，包含 Windows x64 NSIS 安装器、SHA-256 和 manifest，版本、commit 与 tag 一致。
- Release 文案没有宣称代码签名、真实 Provider 或仓库未覆盖的 Windows 安装/卸载验收。
