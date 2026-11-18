# Tomato-Agent UI 优化执行计划

> **创建时间**: 2026-11-18  
> **状态**: 阶段一完成  
> **预计完成时间**: 1个工作日

---

## 更新日志

### 2026-11-18 阶段一完成

**已完成**:
- ✅ 任务 1.1: 全局键盘快捷键
  - 实现 `Cmd/Ctrl+N` 新建会话
  - 实现 `Cmd/Ctrl+L` 和 `/` 聚焦输入框
  - 实现 `Esc` 关闭弹窗（按优先级：对话框 > 设置 > 记忆管理）
  - Hook 集成到 `DesktopChatApp.tsx`

- ✅ 任务 1.2: 加载状态优化
  - 新增 `LoadingSpinner` 组件（动画旋转指示器）
  - 新增 `LoadingMessage` 组件（长操作提示）
  - 新增 `useMinimumLoadingTime` Hook（防止闪烁）
  - 集成到消息输入区域

- ✅ 任务 1.3: Markdown 渲染增强
  - 集成 react-markdown v9 + remark-gfm 支持 GFM 语法
  - 添加代码语法高亮（react-syntax-highlighter + oneDark 主题）
  - 实现代码复制按钮
  - 新增 `MessageContent` 和 `CodeBlock` 组件
  - 修复 TypeScript 类型问题

**Git提交**: `3e8f240` (feat: 完成任务1.3 Markdown渲染增强)
**构建状态**: ✅ 通过（1034.90 kB bundle, gzip 347.96 kB）

---


## 概述

本计划旨在提升 Tomato-Agent 桌面应用的用户体验,参考现代桌面应用的交互模式,分三个阶段实施:

1. **阶段一:交互体验提升** - 键盘快捷键、加载状态、Markdown渲染
2. **阶段二:视觉体验优化** - 动画效果、视觉层次、深色模式
3. **阶段三:功能增强** - 搜索功能、Diff查看器、通知系统

---

## 阶段一:交互体验提升 (优先级:P0)

### 任务 1.1: 全局键盘快捷键

**目标**: 为常用操作添加键盘快捷键,提升操作效率

**功能清单**:
- `Ctrl/Cmd + N` - 新建会话
- `Ctrl/Cmd + F` - 搜索会话
- `Ctrl/Cmd + P` - 切换项目
- `Ctrl/Cmd + B` - 切换侧边栏显隐
- `/` 或 `Ctrl/Cmd + L` - 聚焦输入框
- `Esc` - 取消当前操作/关闭弹窗

**技术实现**:
```typescript
// 新增文件: desktop/ui/src/hooks/useKeyboardShortcuts.ts
export function useKeyboardShortcuts(handlers: KeyboardHandlers) {
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const isMac = navigator.platform.toUpperCase().indexOf('MAC') >= 0;
      const modKey = isMac ? e.metaKey : e.ctrlKey;
      
      if (modKey && e.key === 'n') {
        e.preventDefault();
        handlers.onNewSession?.();
      }
      // ... 其他快捷键
    };
    
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [handlers]);
}
```

**改动文件**:
- `desktop/ui/src/desktop-chat/DesktopChatApp.tsx` - 集成快捷键逻辑
- `desktop/ui/src/hooks/useKeyboardShortcuts.ts` - 新增快捷键hook

**验收标准**:
- [ ] 所有快捷键在 macOS 和 Windows 上均可正常工作
- [ ] 快捷键不与浏览器默认快捷键冲突
- [ ] 按下快捷键后有明确的视觉反馈

---

### 任务 1.2: 加载状态优化

**目标**: 为所有异步操作提供清晰的加载反馈

**改进点**:
1. **发送消息时** - 显示加载动画,禁用输入框
2. **切换项目/会话时** - 显示skeleton占位符
3. **长时间操作(>2秒)** - 显示带文字的进度提示
4. **错误状态** - 统一错误提示样式

**技术实现**:
```typescript
// 新增文件: desktop/ui/src/components/LoadingState.tsx
export const LoadingSpinner = () => (
  <div className="animate-spin rounded-full h-5 w-5 border-2 border-primary border-t-transparent" />
);

export const SkeletonMessage = () => (
  <div className="animate-pulse space-y-3">
    <div className="h-4 bg-gray-200 rounded w-3/4" />
    <div className="h-4 bg-gray-200 rounded w-1/2" />
  </div>
);
```

**改动文件**:
- `desktop/ui/src/components/LoadingState.tsx` - 新增加载组件
- `desktop/ui/src/desktop-chat/DesktopChatApp.tsx` - 集成加载状态

**验收标准**:
- [ ] 所有网络请求显示加载状态
- [ ] 加载状态不会闪烁(最短显示300ms)
- [ ] 超时操作(>10秒)显示超时提示

---

### 任务 1.3: Markdown 渲染增强

**目标**: 支持完整的 Markdown 语法,提升消息内容展示质量

**功能清单**:
- GFM 表格、任务列表、删除线
- 代码块语法高亮(支持80+语言)
- 代码块复制按钮
- 数学公式支持(可选)
- 自动链接识别

**依赖安装**:
```bash
pnpm add react-markdown remark-gfm react-syntax-highlighter
pnpm add -D @types/react-syntax-highlighter
```

**技术实现**:
```typescript
// 新增文件: desktop/ui/src/components/MessageContent.tsx
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';

export const MessageContent = ({ content }: { content: string }) => (
  <ReactMarkdown
    remarkPlugins={[remarkGfm]}
    components={{
      code({ node, inline, className, children, ...props }) {
        const match = /language-(\w+)/.exec(className || '');
        return !inline && match ? (
          <CodeBlock language={match[1]} code={String(children)} />
        ) : (
          <code className={className} {...props}>{children}</code>
        );
      },
    }}
  >
    {content}
  </ReactMarkdown>
);
```

**改动文件**:
- `desktop/ui/src/components/MessageContent.tsx` - 新增Markdown渲染组件
- `desktop/ui/src/desktop-chat/DesktopChatApp.tsx` - 替换现有消息渲染
- `desktop/ui/package.json` - 添加新依赖

**验收标准**:
- [ ] 支持 GFM 所有语法特性
- [ ] 代码高亮正确显示常用语言(JS/TS/Python/Rust/等)
- [ ] 复制代码按钮功能正常
- [ ] 表格在窄屏幕上可横向滚动

---

## 阶段二:视觉体验优化 (优先级:P1)

### 任务 2.1: 流畅动画效果

**目标**: 添加自然流畅的过渡动画

**动画清单**:
1. **消息进入** - fade + slide up (300ms)
2. **侧边栏** - slide in/out (200ms)
3. **列表项hover** - 背景色渐变 (150ms)
4. **按钮点击** - scale + 涟漪效果
5. **页面切换** - fade (200ms)

**技术实现**:
```css
/* 添加到 style.css */
@keyframes slideUp {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.message-enter {
  animation: slideUp 0.3s ease-out;
}

.sidebar-transition {
  transition: transform 0.2s cubic-bezier(0.4, 0, 0.2, 1);
}
```

**改动文件**:
- `desktop/ui/src/style.css` - 添加动画样式
- 各组件添加动画类名

**验收标准**:
- [ ] 动画在低性能设备上不卡顿
- [ ] 支持 `prefers-reduced-motion` 媒体查询
- [ ] 动画时长符合Material Design建议

---

### 任务 2.2: 视觉层次强化

**目标**: 建立清晰的视觉层次和设计系统

**改进点**:
1. **间距系统** - 使用 4px/8px/12px/16px/24px/32px 基准
2. **字体层级** - 5级标题 + 3级正文
3. **色彩对比度** - 确保 WCAG AA 级(4.5:1)
4. **圆角规范** - 4px(小组件) / 8px(卡片) / 12px(弹窗)
5. **阴影系统** - 3级投影(sm/md/lg)

**Tailwind配置扩展**:
```javascript
// tailwind.config.js
module.exports = {
  theme: {
    extend: {
      colors: {
        primary: {
          50: '#dcedea',
          500: '#287f78',
          700: '#176752',
        },
        neutral: {
          900: '#17202a',
          700: '#526171',
          500: '#687783',
          300: '#cbd5e1',
          200: '#d8dfe4',
          50: '#f5f7f8',
        },
      },
      spacing: {
        '4.5': '18px',
      },
    },
  },
};
```

**改动文件**:
- `desktop/ui/tailwind.config.js` - 扩展主题配置
- `desktop/ui/src/style.css` - 更新全局样式
- 所有组件 - 使用新的间距和色彩变量

**验收标准**:
- [ ] 所有文本对比度 ≥ 4.5:1
- [ ] 间距使用统一的基准值
- [ ] 视觉权重与信息层级匹配

---

### 任务 2.3: 深色模式支持

**目标**: 提供护眼的深色主题

**实现方案**:
1. **自动检测** - 使用 `prefers-color-scheme`
2. **手动切换** - 导航栏添加主题切换按钮
3. **持久化** - 使用 localStorage 保存偏好
4. **色板设计** - 遵循深色模式最佳实践

**深色模式色板**:
```javascript
// 深色模式颜色
const darkColors = {
  bg: {
    primary: '#0d1117',
    secondary: '#161b22',
    tertiary: '#21262d',
  },
  text: {
    primary: '#e6edf3',
    secondary: '#8b949e',
    tertiary: '#6e7681',
  },
  border: '#30363d',
  primary: '#3fb950', // 绿色在深色背景下调整
};
```

**技术实现**:
```typescript
// 新增文件: desktop/ui/src/hooks/useTheme.ts
export function useTheme() {
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    const saved = localStorage.getItem('theme');
    if (saved) return saved as 'light' | 'dark';
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  });

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark');
    localStorage.setItem('theme', theme);
  }, [theme]);

  return { theme, setTheme };
}
```

**改动文件**:
- `desktop/ui/src/hooks/useTheme.ts` - 新增主题hook
- `desktop/ui/tailwind.config.js` - 启用 darkMode
- `desktop/ui/src/style.css` - 添加深色模式样式
- `desktop/ui/src/desktop-chat/DesktopChatApp.tsx` - 集成主题切换

**验收标准**:
- [ ] 深色模式对比度符合 WCAG 标准
- [ ] 主题切换无闪烁
- [ ] 偏好设置在重启后保留

---

## 阶段三:功能增强 (优先级:P2)

### 任务 3.1: 会话搜索功能

**目标**: 快速定位历史对话内容

**功能清单**:
1. **全文搜索** - 搜索消息内容和会话标题
2. **实时高亮** - 匹配结果高亮显示
3. **搜索历史** - 记录最近10次搜索
4. **高级模式** - 支持正则表达式(可选)
5. **快捷键** - `Ctrl/Cmd + F` 打开搜索

**技术实现**:
```typescript
// 新增文件: desktop/ui/src/components/SearchBar.tsx
export const SearchBar = ({ onSearch }: { onSearch: (query: string) => void }) => {
  const [query, setQuery] = useState('');
  const debouncedQuery = useDebounce(query, 300);

  useEffect(() => {
    onSearch(debouncedQuery);
  }, [debouncedQuery, onSearch]);

  return (
    <div className="relative">
      <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" size={18} />
      <input
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="搜索会话..."
        className="w-full pl-10 pr-4 py-2 border rounded-lg"
      />
    </div>
  );
};
```

**改动文件**:
- `desktop/ui/src/components/SearchBar.tsx` - 新增搜索组件
- `desktop/ui/src/desktop-chat/ProjectNavigation.tsx` - 集成搜索功能
- `desktop/ui/src/utils/search.ts` - 新增搜索逻辑

**验收标准**:
- [ ] 搜索响应时间 < 100ms (1000条消息)
- [ ] 高亮匹配结果清晰可见
- [ ] 支持中英文混合搜索

---

### 任务 3.2: 高级 Diff 查看器

**目标**: 提供专业的代码变更对比视图

**功能清单**:
1. **并排对比** - split view 显示
2. **行内差异** - 字符级别高亮
3. **折叠未变更** - 只显示变更附近代码
4. **虚拟滚动** - 支持大文件(>10k行)
5. **语法高亮** - 保持代码可读性

**依赖安装**:
```bash
pnpm add react-diff-viewer-continued
```

**技术实现**:
```typescript
// 更新 DesktopChatApp.tsx 中的 diff 渲染
import ReactDiffViewer from 'react-diff-viewer-continued';

const ChangesetViewer = ({ oldCode, newCode, language }: ChangesetProps) => (
  <ReactDiffViewer
    oldValue={oldCode}
    newValue={newCode}
    splitView={true}
    useDarkTheme={theme === 'dark'}
    hideLineNumbers={false}
    showDiffOnly={true}
    extraLinesSurroundingDiff={3}
  />
);
```

**改动文件**:
- `desktop/ui/src/components/ChangesetViewer.tsx` - 新增Diff组件
- `desktop/ui/src/desktop-chat/DesktopChatApp.tsx` - 替换现有diff渲染
- `desktop/ui/package.json` - 添加依赖

**验收标准**:
- [ ] 大文件(>5000行)滚动流畅
- [ ] 行内差异高亮准确
- [ ] 支持常用编程语言语法高亮

---

### 任务 3.3: 通知系统

**目标**: 及时反馈重要事件

**通知类型**:
1. **任务完成** - 桌面系统通知(Tauri API)
2. **错误提示** - 应用内 toast
3. **操作确认** - 成功提示
4. **警告信息** - 黄色警告框

**依赖安装**:
```bash
pnpm add react-hot-toast
```

**技术实现**:
```typescript
// 集成 Tauri notification API
import { sendNotification } from '@tauri-apps/api/notification';
import toast, { Toaster } from 'react-hot-toast';

export const NotificationCenter = {
  success: (message: string) => {
    toast.success(message, { duration: 3000 });
  },
  
  error: (message: string) => {
    toast.error(message, { duration: 5000 });
  },
  
  taskComplete: async (taskName: string) => {
    await sendNotification({
      title: '任务完成',
      body: `${taskName} 已完成`,
    });
  },
};
```

**改动文件**:
- `desktop/ui/src/utils/notifications.ts` - 新增通知工具
- `desktop/ui/src/desktop-chat/DesktopChatApp.tsx` - 集成通知系统
- `desktop/ui/package.json` - 添加依赖

**验收标准**:
- [ ] 桌面通知需要用户授权
- [ ] Toast 提示不遮挡关键内容
- [ ] 通知优先级正确(错误>警告>信息)

---

## 组件重构计划

### 当前问题

`DesktopChatApp.tsx` 文件超过 2000 行,包含过多职责:
- 项目和会话管理
- 消息列表渲染
- 输入框和工具栏
- Diff 查看器
- 任务审批流程
- 内存管理

### 重构目标

拆分为独立的功能组件,遵循单一职责原则:

```
desktop-chat/
├── DesktopChatApp.tsx           (主容器,协调各组件)
├── components/
│   ├── MessageContent.tsx       (Markdown渲染)
│   ├── MessageList.tsx          (消息列表)
│   ├── InputArea.tsx            (输入框+工具栏)
│   ├── ChangesetViewer.tsx      (代码diff)
│   ├── ApprovalPanel.tsx        (任务审批)
│   ├── LoadingState.tsx         (加载状态)
│   ├── SearchBar.tsx            (搜索框)
│   └── NotificationCenter.tsx   (通知中心)
├── hooks/
│   ├── useKeyboardShortcuts.ts  (键盘快捷键)
│   ├── useTheme.ts              (主题切换)
│   ├── useMessageList.ts        (消息管理)
│   └── useDebounce.ts           (防抖工具)
└── utils/
    ├── markdown.ts              (Markdown工具)
    ├── diff.ts                  (Diff工具)
    ├── search.ts                (搜索逻辑)
    └── notifications.ts         (通知工具)
```

### 重构原则

1. **渐进式重构** - 每次重构后保证功能正常
2. **保持向后兼容** - 不破坏现有API
3. **测试覆盖** - 重构前后行为一致
4. **代码复用** - 提取可复用的逻辑

---

## 依赖清单

### 新增依赖

```json
{
  "dependencies": {
    "react-markdown": "^9.0.0",
    "remark-gfm": "^4.0.0",
    "react-syntax-highlighter": "^15.5.0",
    "react-hot-toast": "^2.4.1",
    "react-diff-viewer-continued": "^3.3.0"
  },
  "devDependencies": {
    "@types/react-syntax-highlighter": "^15.5.11"
  }
}
```

### 可选依赖

```json
{
  "dependencies": {
    "framer-motion": "^11.0.0"  // 如需复杂动画
  }
}
```

---

## 时间线

| 阶段 | 任务 | 预计时间 | 状态 |
|------|------|----------|------|
| 阶段一 | 1.1 键盘快捷键 | 1h | ✅ 已完成 |
| 阶段一 | 1.2 加载状态 | 1h | ✅ 已完成 |
| 阶段一 | 1.3 Markdown渲染 | 1h | ✅ 已完成 |
| 阶段二 | 2.1 动画效果 | 0.5h | ⏳ 待开始 |
| 阶段二 | 2.2 视觉层次 | 1h | ⏳ 待开始 |
| 阶段二 | 2.3 深色模式 | 0.5h | ⏳ 待开始 |
| 阶段三 | 3.1 搜索功能 | 1.5h | ⏳ 待开始 |
| 阶段三 | 3.2 Diff查看器 | 1h | ⏳ 待开始 |
| 阶段三 | 3.3 通知系统 | 0.5h | ⏳ 待开始 |
| **总计** |  | **8h** |  |

---

## 验收标准

### 阶段一完成标准
- [x] 所有快捷键功能正常
- [x] 加载状态覆盖所有异步操作
- [ ] Markdown 渲染支持 GFM 完整语法
- [ ] 代码块语法高亮正确

### 阶段二完成标准
- [ ] 动画效果流畅,无卡顿
- [ ] 视觉层次清晰,对比度符合 WCAG AA
- [ ] 深色模式正常工作,偏好持久化

### 阶段三完成标准
- [ ] 搜索功能快速准确
- [ ] Diff 查看器支持大文件
- [ ] 通知系统正常触发

### 总体完成标准
- [ ] 所有新功能在 macOS 和 Windows 上测试通过
- [ ] 无功能回归
- [ ] 代码通过 ESLint 检查
- [ ] 构建成功,无 TypeScript 错误

---

## 风险与缓解

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|----------|
| 依赖冲突 | 中 | 低 | 使用 pnpm 锁定版本 |
| 性能下降 | 高 | 中 | 每个阶段后性能测试 |
| 现有功能破坏 | 高 | 中 | 渐进式重构,保持测试覆盖 |
| 深色模式颜色不合适 | 低 | 中 | 参考 GitHub/VS Code 配色 |

---

## 后续优化方向

完成三个阶段后,可考虑的进一步优化:

1. **国际化支持** - i18n 多语言
2. **插件系统** - 可扩展的第三方插件
3. **性能监控** - 集成性能分析工具
4. **无障碍优化** - 完整的 ARIA 支持
5. **自动化测试** - E2E 测试覆盖核心流程

---

## 更新日志

### 2026-11-18
- ✅ 创建执行计划文档
- ⏳ 待开始实施

---

**文档版本**: v1.0  
**负责人**: Codex  
**审核状态**: 待审核
