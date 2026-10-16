# 分层验证与 CI 说明

仓库统一入口为 `python3 scripts/verify.py`，需在 Linux/WSL 环境运行。后端固定使用 `backend/.venv/bin/python`；前端使用 PATH 中的 Linux Node 与 pnpm。前端版本以 `frontend/package.json` 的 `packageManager`（pnpm 10.34.5）为准。

| 层级 | 命令示例 | 结论范围 |
| --- | --- | --- |
| 快速反馈 | `python3 scripts/verify.py quick --static --backend-test tests/test_assistant_api.py` | Python 语法、TypeScript/Vite 构建及所选测试；尚无专门格式检查器 |
| 模块回归 | `python3 scripts/verify.py module assistant execution rag writing frontend --timeout 600` | 注册模块测试映射；不等同于全仓测试 |
| 集成验证 | `python3 scripts/verify.py integration --timeout 600` | 真实 SQLite、Worker 生命周期/恢复、ASGI API 与 Node 前端测试；不等同于真实浏览器端到端 |
| 外部验收 | `python3 scripts/verify.py external docker --confirm-real-dependencies` | 仅被显式选择的 Docker、OpenShell 或 Provider 环境 |

每次运行在 `verification-artifacts/<run-id>/` 写步骤日志与 `result.json`，记录代码 commit/dirty、命令、开始/结束时间、状态、fake/real 环境及工件路径。当前 `skipped` 仅记录入口主动跳过项，pytest 内部 skip 需查看日志，不能据退出码推断全部场景执行。常规集成的 Provider 为 fake，SQLite 为真实文件；集成 pytest 临时数据位于该次工件目录。RAG 测试内独立快照仍按仓库规则写入 `backend/data/rag/`。

## GitHub Actions

`.github/workflows/verification.yml` 在 push 和 pull request 时触发，使用 Ubuntu runner、Python 3.12、Node 22 和 pnpm 10.34.5，创建 `backend/.venv` 并安装依赖，运行入口单测、模块回归与集成门禁，最后上传验证工件。模块中的前端步骤包括 TypeScript/Vite 构建。CI 不读取本机 `.env`、本机虚拟环境或 Windows PATH；需要网络访问依赖仓库。后端依赖当前通过 pyproject 范围安装，并非锁定安装。

工作流文件提交并 push 后，进入 GitHub 仓库 Actions 页面查看 Verification。首次远端运行前只能称“工作流已配置”，不能称“远端 CI 已通过”。若失败，查看对应步骤与上传的验证日志。当前未配置 `workflow_dispatch`，手动首次触发可通过 push；本地 commit 本身不会触发 Actions。仓库分支保护是否强制该检查，需要另行在 GitHub 设置。

## 首次 CI 前本地检查（2026-10-16）

入口单测 4 项、Provider 客户端/记录器 17 项、assistant 模块 285 项通过；集成后端 80 项、API 12 项及 Node 测试通过。修正了 Runtime schema 接入后 Docker/队列测试替身未透传 `response_format` 的问题。execution 回归发现 `test_unsafe_or_unsuccessful_execution_never_completes[escape-failed-permission_denied]` 稳定失败（实际 `execution_failed`），该模块其余 208 项通过、7 项显式外部验收跳过。首次 workflow 可能在此处失败，不能将本次提交描述为全部门禁通过。

writing 105 项、frontend 31 项与 pnpm build 通过。RAG 为 320 passed / 2 failed：`test_answer_runner_real_llm_is_opt_in_and_client_factory_is_offline_by_default` 和 `test_reranker_settings_have_safe_disabled_defaults_and_validate_limits`；日志显示本机 Provider/Reranker 配置影响默认值断言，尚未证明入口可完全隔离本机配置。未发起真实 Provider 验收。

## WSL 工具路径

运行 `command -v node pnpm`，路径应在 `/usr/` 或 `/home/` 下，避免选中 `/mnt/c/`、`/mnt/d/` 的 Windows shim。`backend/.venv` 已存在且解释器可用；此前旧质量工具失败是错误寻找根 `.venv`。用户已确认 Linux pnpm 10.34.5 安装依赖与构建通过。

外部验收不纳入常规 CI，不自动启动 Provider、Chrome CDP、Docker 或 OpenShell。外部包装超时最大 600 秒；Provider 预算最多 4 Run/16 请求。进程子树清理、pytest skip 结构化聚合及常规真实浏览器门禁仍需后续完善，不将现有 Node/ASGI 通过扩大为这些能力已验收。