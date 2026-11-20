# Windows Codex Desktop 前端分析

分析对象：`OpenAI.Codex_26.1111.2228.0_x64__2p2nqsd0c76g0`

安装包位置：

`C:\Program Files\WindowsApps\OpenAI.Codex_26.1111.2228.0_x64__2p2nqsd0c76g0`

读取方式：只读查看 Windows AppX 包，并使用 `@electron/asar` 从
`app/resources/app.asar` 提取少量入口文件。未修改安装包，也未启动或注入程序。

## 结构

- 宿主：Chromium/Electron，主程序为 `app/ChatGPT.exe`。
- Electron 主进程入口：`.vite/build/early-bootstrap.js`。
- Web 前端入口：`webview/index.html`。
- Web 前端入口加载 5 个哈希 chunk，其中主逻辑集中在
  `webview/assets/348410.374a9b8f98.js`。
- 原生核心与命令执行组件以独立 Windows/ELF 二进制放在
  `app/resources/`，不是前端 JavaScript。

## 可参考的交互边界

入口 chunk 中可确认的路由包括：

- `/hotkey-window`
- `/new-thread`
- `/thread/:conversationId`（代码中也有 `/:conversationId` 形式）
- `/code-review`
- `/local`
- `/remote`
- `/settings/plugins`
- `/skills/manage`
- `/skills/plugins`
- `/plugins`
- `/share`
- `/mcp-app`

前端持久化/状态键包括活动工作区、保存的工作区根目录、项目与线程归属、
置顶线程、项目排序、远程主机工作目录、无项目线程等。这说明侧栏不是单纯
的展示组件，而是由工作区、项目、线程三层状态共同驱动。

`preload.js` 暴露的 `electronBridge` 主要能力包括：

- 获取系统主题、构建 flavor、Sentry 初始化信息和初始侧栏 bootstrap；
- 在 view 与主进程间发送/订阅消息；
- 连接 Git worker 和其他 worker；
- 获取文件真实路径、开始文件/链接拖拽；
- 打开原生上下文菜单；
- 处理分块消息、远程视频画中画和 app-host MessagePort。

## 当前提取物

- `index.html`
- `package.json`
- `903838.979fd3f34c.js`
- `203305.36534d55d5.js`
- `727403.da6b00f04b.js`
- `936243.4882e1db4f.js`
- `348410.374a9b8f98.js`
- `preload.js`
- `browser-page-preload.js`
- `early-bootstrap.js`
- `app-asar-list.txt`

这些文件是压缩后的发布产物，没有发现对应 source map；可以用于接口名、路由、
状态键和消息边界参考，不能还原原始 TypeScript/React 组件结构。

## 版本边界

`package.json` 的应用版本是 `26.1111.21308`，外层 Windows AppX 版本是
`26.1111.2228.0`。后续升级后应重新生成列表和入口文件，避免把旧版交互当成
当前行为。
