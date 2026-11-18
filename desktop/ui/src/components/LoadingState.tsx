import type { ReactNode } from 'react';

/**
 * 加载动画组件 - 旋转的圆环
 */
export function LoadingSpinner({ className = '' }: { className?: string }) {
  return (
    <div
      className={`inline-block h-5 w-5 animate-spin rounded-full border-2 border-primary border-t-transparent ${className}`}
      role="status"
      aria-label="加载中"
    />
  );
}

/**
 * 骨架屏消息组件 - 用于消息加载时的占位符
 */
export function SkeletonMessage() {
  return (
    <div className="animate-pulse space-y-3 py-4" role="status" aria-label="内容加载中">
      <div className="h-4 bg-gray-200 dark:bg-gray-700 rounded w-3/4" />
      <div className="h-4 bg-gray-200 dark:bg-gray-700 rounded w-1/2" />
      <div className="h-4 bg-gray-200 dark:bg-gray-700 rounded w-5/6" />
    </div>
  );
}

/**
 * 带文字的加载提示 - 用于长时间操作
 */
export function LoadingMessage({ message, children }: { message?: string; children?: ReactNode }) {
  return (
    <div className="flex items-center gap-3 py-4" role="status">
      <LoadingSpinner />
      <span className="text-gray-600 dark:text-gray-400">
        {message || children || '加载中...'}
      </span>
    </div>
  );
}

/**
 * 防止闪烁的加载状态管理 Hook
 * 确保加载状态至少显示指定时长，避免短时间操作导致的闪烁
 */
export function useMinimumLoadingTime(isLoading: boolean, minimumMs = 300) {
  const [showLoading, setShowLoading] = useState(false);
  const loadingStartTime = useRef<number | null>(null);

  useEffect(() => {
    if (isLoading) {
      // 开始加载
      loadingStartTime.current = Date.now();
      setShowLoading(true);
    } else if (loadingStartTime.current !== null) {
      // 结束加载，但确保最小显示时间
      const elapsed = Date.now() - loadingStartTime.current;
      const remaining = minimumMs - elapsed;

      if (remaining > 0) {
        const timer = setTimeout(() => {
          setShowLoading(false);
          loadingStartTime.current = null;
        }, remaining);
        return () => clearTimeout(timer);
      } else {
        setShowLoading(false);
        loadingStartTime.current = null;
      }
    }
  }, [isLoading, minimumMs]);

  return showLoading;
}

// 导入必需的 React hooks
import { useEffect, useRef, useState } from 'react';
