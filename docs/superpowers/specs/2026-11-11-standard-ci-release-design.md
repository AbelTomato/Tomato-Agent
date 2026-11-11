# 标准 CI/CD 与 Windows Release 设计

## 目标

将当前面向 `v0.1.0` 的 GitHub Actions 流程扩展为可重复的 Windows x64 桌面发布流程。PR 和 push 继续由 CI 验证；版本 tag 触发构建并创建 GitHub Draft Release，由维护者检查资产和安装结果后手动发布。

## 现状

- `.github/workflows/verification.yml` 在 Ubuntu 执行 Python、UI 和 integration 验证，在 Windows 执行 Rust 测试。
- `.github/workflows/release.yml` 已能从 Windows runner 构建 NSIS 安装器并生成 SHA-256 与 manifest。
- Release tag、安装器文件名、manifest 字段和 Release 说明路径仍写死为 `v0.1.0`。
- tag 发布工作流会自动把 Draft 改为公开 Release；这不符合小范围分发所需的人工检查门槛。
- CI、GitHub Release 和本地 Windows 打包脚本使用浮动的 Python 依赖解析；仓库已有的 `backend/uv.lock` 尚未被这些构建路径使用。

## 决策

1. 保留 GitHub Actions、Windows x64 和 NSIS 安装包，不增加新发布平台。
2. 使用稳定版 SemVer tag，格式为 `vMAJOR.MINOR.PATCH`，且必须与 `desktop/src-tauri/tauri.conf.json` 的版本一致；Cargo crate 版本也必须一致。
3. Release notes 按 tag 保存为 `docs/releases/vMAJOR.MINOR.PATCH.md`。缺少对应说明时，正式 tag 构建失败，不生成不完整 Draft。
4. tag 构建验证下载资产与 manifest 后创建或更新 Draft Release。重跑允许更新同一 tag 的 Draft 资产；已公开的 Release 不可被覆盖。
5. tag 工作流不公开 Release。维护者在 GitHub 上检查变更说明、SHA-256、manifest 和 Windows 安装结果后手动发布。
6. `workflow_dispatch` 只构建并上传短期 Actions artifact，不创建或修改 GitHub Release。
7. 保留 `contents: read` 的默认权限；只有创建/更新 Draft 的 job 获得 `contents: write`。不加入代码签名密钥或真实 Provider 凭据。
8. Python runtime、test 和 Windows packaging 依赖由 `backend/uv.lock` 锁定；PyInstaller 固定在 build dependency group 中。CI、Release workflow 和本地 Windows 打包脚本均使用同一锁定环境。
9. 本地 Windows 打包脚本的产品版本和 NSIS 安装器路径从 Tauri 配置读取，遵循与 GitHub Release 相同的版本契约。
10. 本地 Windows 打包从 WSL UNC 源码目录运行时，将 uv cache 与临时目录固定在 Windows 本地构建盘，避免跨文件系统原子移动失败。

## 验收

- PR CI 执行 Release 版本契约测试；现有 Python、UI、integration 和 Windows Rust gate 仍运行。
- Ubuntu CI、Windows Release 和本地 Windows build-release 均使用锁定的 Python runtime/test/build 依赖。
- release workflow 接受 `v*` tag 事件，但在构建前拒绝无效 SemVer、tag/config/Cargo 版本不一致及缺失的 Release notes。
- 构建出的安装器、校验文件和 manifest 均由版本及触发 commit 动态生成；Dry-run 的 manifest 明确没有 release tag。
- 正式 tag 路径只创建或更新 Draft；重复运行可安全更新 Draft；已公开 Release 会被拒绝修改。
- 本地验证 workflow YAML、版本校验测试、现有 CI 等价测试及 `git diff --check`。不从当前工作区推 tag 或公开 Release。

## 暂不处理

代码签名、自动更新、多架构构建、自动发布审批环境、SBOM、自动变更日志生成及远端分支保护设置。发布说明保持人工编写；分支保护需在 GitHub 仓库设置中配置，不能由本地 workflow 变更代替。
