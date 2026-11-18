import { useEffect } from 'react';

export interface KeyboardHandlers {
  onNewSession?: () => void;
  onSearch?: () => void;
  onToggleProject?: () => void;
  onToggleSidebar?: () => void;
  onFocusInput?: () => void;
  onEscape?: () => void;
}

/**
 * 全局键盘快捷键 Hook
 * 
 * 支持的快捷键:
 * - Ctrl/Cmd + N: 新建会话
 * - Ctrl/Cmd + F: 搜索会话
 * - Ctrl/Cmd + P: 切换项目
 * - Ctrl/Cmd + B: 切换侧边栏
 * - Ctrl/Cmd + L 或 /: 聚焦输入框
 * - Esc: 取消/关闭
 */
export function useKeyboardShortcuts(handlers: KeyboardHandlers) {
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // 检测操作系统并确定修饰键
      const isMac = navigator.platform.toUpperCase().indexOf('MAC') >= 0;
      const modKey = isMac ? e.metaKey : e.ctrlKey;

      // 如果在输入框中,某些快捷键需要特殊处理
      const target = e.target as HTMLElement;
      const inInput = target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable;

      // Esc - 总是可用
      if (e.key === 'Escape') {
        e.preventDefault();
        handlers.onEscape?.();
        return;
      }

      // / - 聚焦输入框 (不在输入框中时)
      if (e.key === '/' && !inInput) {
        e.preventDefault();
        handlers.onFocusInput?.();
        return;
      }

      // 需要修饰键的快捷键
      if (!modKey) return;

      switch (e.key.toLowerCase()) {
        case 'n':
          e.preventDefault();
          handlers.onNewSession?.();
          break;

        case 'f':
          e.preventDefault();
          handlers.onSearch?.();
          break;

        case 'p':
          e.preventDefault();
          handlers.onToggleProject?.();
          break;

        case 'b':
          e.preventDefault();
          handlers.onToggleSidebar?.();
          break;

        case 'l':
          e.preventDefault();
          handlers.onFocusInput?.();
          break;

        default:
          // 不处理其他键
          break;
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [handlers]);
}
