# Windows 普通聊天使用说明

本文适用于 Tomato Agent Windows 普通聊天版（chat-only）。该版本在桌面进程内运行，不要求安装 Python、WSL 或外部服务。阶段4已在 Windows 11 当前用户环境完成验收，真实 Provider 对话闭环通过；干净 Windows 用户环境及卸载后数据保留按用户决定未执行，仍未验证。

## 系统要求

- Windows 10 或 Windows 11 x64。
- 可访问配置的 OpenAI Chat Completions 兼容 API；使用 HTTPS 端点时需能正常验证服务端证书。
- 本地安装器与应用均未签名；Windows SmartScreen 可能显示来源提示。

## 首次配置

1. 安装并启动 Tomato Agent。首次启动进入普通聊天界面。
2. 打开模型设置，填写 API Base URL、模型名称和 API Key。Base URL 应为服务提供方要求的兼容 API 地址；不要附加聊天补全路径，除非服务方明确要求。
3. 使用“测试连接”确认端点、凭据和模型可用，再保存设置。
4. 创建会话并发送纯文本消息。应用支持多个本地会话、流式回复、停止生成和会话历史恢复。

API Key 保存于当前 Windows 用户的 Windows Credential Manager，不写入聊天数据库或 `model.json`。端点和模型名称保存在本地设置文件。请勿将 API Key 发给支持人员或写入诊断材料。

## 本地数据与备份

当前 Windows 构建的聊天数据位于 `%APPDATA%\dev.tomato.agent\TomatoAgent\desktop-chat\`：

- `chat.sqlite3`：会话和消息历史（SQLite）。
- `model.json`：API Base URL 和模型名称，不含 API Key。
- SQLite 运行时可能同时产生 `chat.sqlite3-wal` 和 `chat.sqlite3-shm`。

实际路径以前缀 `%APPDATA%\dev.tomato.agent` 为准；它来自 Tauri 应用标识 `dev.tomato.agent`，后续若应用标识变更，数据目录前缀也会变化。备份前先完全退出应用，再复制整个 `desktop-chat` 目录到仅本人可访问的安全位置。恢复时先退出应用，再将备份目录内容放回同一数据目录。API Key 不包含在文件备份中；重装或迁移到另一台设备后需在 Credential Manager 中重新配置。不要在应用运行时只复制主数据库文件，以免遗漏 WAL 中尚未合并的数据。

## 网络与请求错误

- 未配置模型时不能发送消息；请先完成设置并测试连接。
- DNS、代理、防火墙、TLS 或超时错误出现时，先检查网络和 API Base URL，再通过设置页显式测试连接。
- Provider 连接测试和单次聊天请求的总超时均为 120 秒；响应或流式生成超过该时限会失败。较慢的渠道应预留足够响应时间。
- 认证错误请核对 API Key；模型或 Provider 错误请核对模型名、账户额度和服务方兼容协议。
- 聊天请求不会自动重试。失败或停止的部分回复不会作为完整 assistant 消息保存；确认失败原因后再由用户手动重试。
- 测试连接和聊天请求都会联系所配置的 Provider。按 Provider 的计费与隐私条款使用。

## 更新与卸载

卸载前建议完全退出应用并备份 `%APPDATA%\dev.tomato.agent\TomatoAgent\desktop-chat\`。NSIS 卸载流程不应静默删除用户数据；但当前尚未在干净 Windows 环境完成卸载后数据保留验收，因此卸载前备份仍是必要措施。卸载不会迁移会话或凭据。删除应用后若需清除数据，应先确认备份与应用均不再需要，再由用户自行删除上述目录，并通过 Windows Credential Manager 移除 Tomato Agent 的凭据。

## 验收状态

2026-11-08 已在 Windows 11 当前用户环境实测安装并启动。修复 TLS 和 Windows 系统代理支持后，真实 Provider 连接测试、流式回复、多轮上下文、停止生成、网络错误提示和重启后历史恢复均通过。该用户配置已有数据；按用户决定未执行干净用户环境及卸载后数据保留验收，因此这两项仍未验证，不能据本次结果作出发布环境兼容或卸载行为结论。

## 后续补做干净环境验收时

阶段4真实 Provider 与当前 Windows 用户的产品功能验收已经完成。用户决定不为本次验收准备干净 Windows 配置，因此首次安装隔离及卸载后数据/凭据保留不在已完成范围内，也没有通过结论。若日后单独补验这两项，再准备干净的非管理员 Windows 用户配置，并先确认 Provider 请求预算；单次请求最多运行 120 秒且未限制最大输出 token 数，连接测试也会发送真实请求。安装器归档及哈希记录见阶段4验收日志。
