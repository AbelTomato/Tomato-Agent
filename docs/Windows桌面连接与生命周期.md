# Windows 桌面连接与生命周期

2026-10-19：桌面实施计划切片3后端工程阶段完成。WSL 桌面启动通过受控 stdin 将发行版名作为 `environment_id` 传入；项目 SQLite 记录由后端 `.desktop-instance` 绑定 `service_instance_id`，并通过配置的 `workspace_allowed_roots` 限定浏览、打开与新建。普通项目登记可不配置 pytest；无测试 profile 的工作区仍不能运行测试。项目 API 为 `/api/projects` 与 `/api/projects/directories`。真实 Windows UI 项目流程尚未实现，属于后续切片。此前任务2连接边界依然有效。

任务1技术选型依据见 [Windows桌面技术验证](Windows桌面技术验证.md)。

## 启动和连接

Windows 窗口中点击“发现 WSL 发行版”，在高级连接设置填写已有后端的 WSL 绝对路径，再点击“连接 / 重连助手服务”。要求该目录已有 `.venv/bin/python` 和项目依赖。不自动安装、部署或修改 `.env`。普通 Web 启动仍使用 `app.main:app`。

原生层固定调用 `wsl.exe --distribution <发行版> --cd <后端目录> --exec <后端目录>/.venv/bin/python -m app.desktop.bootstrap`，不经过 shell。发行版必须来自发现结果，路径作为独立 argv。令牌由 Windows 凭据管理器按环境/后端目录保存，通过 stdin 注入，不传入 WebView、URL、命令行或日志。模型密钥设置不在本任务范围。

桌面业务启动的 stdin JSON 同时携带已发现的发行版名称 `environment_id`、连接凭据与启动所有权信息；该值不在命令行或日志出现。服务实例身份来自 WSL 数据目录的 `.desktop-instance`，不可由客户端覆盖。`desktop_environment_id` 和 `desktop_service_instance_id` 是后端应用配置项；日常桌面启动分别由原生连接和服务端身份提供。项目记录以三元组 `(environment_id, service_instance_id, root_path)` 区分；同一路径在不同发行版或实例中是不同项目。

项目操作使用 WSL 后端服务解释的 POSIX 路径。`GET /api/projects/directories?path=<absolute-path>` 只列直接子目录，默认隐藏点目录、忽略符号链接并限制最多500项；路径必须落入明确配置的 `workspace_allowed_roots`，不能使用 `/` 作为允许根。`POST /api/projects/open` 可信任并登记已有目录，重复打开返回既有项目身份；`POST /api/projects` 在已批准父目录下创建单层目录，不覆盖冲突对象。打开或创建项目只关联现有 workspace，不授权 shell，也不自动配置测试。

旧 `POST /api/workspaces` 支持 `pytest_paths: []` 或省略字段用于普通目录读取。空 profile 不会生成伪造的 `tests` 目标；`run_tests` 仍由服务端 `TestTargetRegistry` 拒绝。原有非空 profile 校验与代码任务 API 保持不变。创建目录后若 SQLite 项目记录失败，实际目录保留以供核查，不自动删除。

Windows 先选择本机可绑定端口，经 stdin 交给 WSL 绑定 `127.0.0.1`，避免随机 WSL 端口落入 Windows 排除段。实际仍须通过认证与实例核验；竞争或连接失败明确报错，不改变防火墙、监听公网或自动重启服务。原生启动等待20秒，WSL启动等待18秒，单次HTTP请求30秒；失败不循环启动或重发业务提交。

后端数据目录使用 `.desktop-launch.lock` 串行化启动，`.desktop-service.json` 记录端口与实例，`.desktop-instance` 保存数据目录的稳定实例身份。连接清单不含令牌。只有明确连接拒绝才允许重新启动；认证失败、协议不符及身份冲突保留现场并拒绝接管。桌面模式全部 HTTP 路由，包括健康、文档、会话、任务、目录和后续设置，统一要求令牌与 `X-Tomato-Instance`。原生代理仅支持本服务 `/api/` 路径的 GET/POST，禁用重定向。普通 Web 模式不接入这套认证。

## 断开、重连和退出

连接状态为 `disconnected/connecting/ready/error`。错误提供摘要和可展开详情。连接成功重新读取服务端会话与任务，不提交替代 Run；运行中的任务固定原环境。切换环境前显式断开，数据不会自动迁移。

关闭窗口展示“返回应用”和“保留后台任务并退出”。queued/running/waiting 均阻止服务退出；退出客户端不取消任务，不删除 SQLite 事实。显式取消仍走原任务动作。只有当前客户端持有该次启动的所有权、无活动任务且其他客户端均已显式断开，服务才能受控退出。其他窗口连接同实例属于外部连接，不能结束服务。不调用 `wsl --shutdown`，不按 PID 猜测并终止其他服务。

异常客户端丢失采用保守策略：未显式断开的连接保留，会阻止自动关闭；所有者退出后重新打开的客户端也不继承终止权。此时可能需要人工核查遗留服务。WSL 关机后的所有任务自动接管不在本期承诺内。

## 验证证据

- 后端目标回归64/64：认证18项、真实子进程单实例启动/重连2项，以及应用/Worker/统一任务回归；SQLite 集成覆盖 completed/waiting/running/queued 断开后的事实保留。
- 前端相关测试23/23：原生传输不回退浏览器、不重试提交；连接失败、手动重连和活动任务退出确认；既有会话17项。TypeScript/Vite构建通过。
- Windows原生12/12；Windows MSVC release及NSIS构建最终退出码0。沿用任务1已记录的Rust1.99.1、Tauri CLI2.12.3及Windows工具链。
- Windows原生双客户端报告通过：重复attach保持2个连接、共享会话可读、外部close不关闭服务、最后自有空闲close关闭服务。
- Windows UI Automation实际窗口通过：WSL发现、连接、共享会话页面、关窗确认、返回应用、再次关窗并受控退出。Tauri仅授予主窗口事件listen/unlisten权限。该结果为自动化实机观察，不冒充完整人工产品验收。
- 工件保存在本地 `backend/data/desktop-acceptance/task2-20261018/`，包括 `native-lifecycle-final.json`、`ui-final-result.txt`、`windows-package-final.log`，保持Git忽略。所有验收使用独立数据，无真实Provider请求。

实机发现并修复：WSL随机端口44685位于Windows排除段44647–44746；关窗事件缺少Tauri capability。首次事件修复后打包与窗口验证并行导致exe被占用，关闭验收窗口后最终打包退出码0。最终指纹以末次构建为准，任务1和中间构建指纹只表示对应历史产物。任务2未重新执行安装器安装；安装行为已有任务1证据。

复现命令在 [桌面工程说明](../desktop/README.md)。验收入口 `--lifecycle-report <发行版> <WSL后端目录> <独立WSL数据目录> <Windows报告路径>` 使用与UI相同的原生桥；`--acceptance-data <独立WSL数据目录>` 启动隔离验收窗口。日常启动不带验收参数。

最终Windows产物（`C:\Users\AbelTomato\AppData\Local\TomatoAgent\task1-target\release\`）SHA-256：

| 相对产物路径 | SHA-256 |
| --- | --- |
| `tomato-agent-desktop.exe` | `d9a1f26e949b62296d5158a46436efe430cc451ccc99bc54640fba79f94edc04` |
| `bundle\nsis\Tomato Agent_0.1.0_x64-setup.exe` | `941d55d4fb1023e8003229cbfa7bc51822e3f7f6cdb9b1b6df45253290e93349` |