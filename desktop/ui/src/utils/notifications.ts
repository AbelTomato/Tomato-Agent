import toast from 'react-hot-toast';

/**
 * 统一的通知系统，提供非侵入式的操作反馈
 */
export const notify = {
  /**
   * 成功提示（3秒自动关闭）
   */
  success: (message: string) => {
    return toast.success(message, {
      duration: 3000,
      position: 'top-right',
      style: {
        background: 'var(--color-bg-primary)',
        color: 'var(--color-text-primary)',
        border: '1px solid var(--color-success)',
      },
    });
  },

  /**
   * 错误提示（5秒自动关闭）
   */
  error: (message: string) => {
    return toast.error(message, {
      duration: 5000,
      position: 'top-right',
      style: {
        background: 'var(--color-bg-primary)',
        color: 'var(--color-text-primary)',
        border: '1px solid var(--color-error)',
      },
    });
  },

  /**
   * 加载提示（需手动关闭）
   */
  loading: (message: string) => {
    return toast.loading(message, {
      position: 'top-right',
      style: {
        background: 'var(--color-bg-primary)',
        color: 'var(--color-text-primary)',
        border: '1px solid var(--color-border-primary)',
      },
    });
  },

  /**
   * 信息提示（4秒自动关闭）
   */
  info: (message: string) => {
    return toast(message, {
      duration: 4000,
      position: 'top-right',
      icon: 'ℹ️',
      style: {
        background: 'var(--color-bg-primary)',
        color: 'var(--color-text-primary)',
        border: '1px solid var(--color-primary)',
      },
    });
  },

  /**
   * 警告提示（4秒自动关闭）
   */
  warning: (message: string) => {
    return toast(message, {
      duration: 4000,
      position: 'top-right',
      icon: '⚠️',
      style: {
        background: 'var(--color-bg-primary)',
        color: 'var(--color-text-primary)',
        border: '1px solid var(--color-warning)',
      },
    });
  },

  /**
   * 关闭指定的通知
   */
  dismiss: (toastId: string) => {
    toast.dismiss(toastId);
  },

  /**
   * 关闭所有通知
   */
  dismissAll: () => {
    toast.dismiss();
  },

  /**
   * Promise通知（自动处理加载->成功/失败状态）
   */
  promise: <T,>(
    promise: Promise<T>,
    messages: {
      loading: string;
      success: string | ((data: T) => string);
      error: string | ((error: any) => string);
    }
  ) => {
    return toast.promise(
      promise,
      {
        loading: messages.loading,
        success: messages.success,
        error: messages.error,
      },
      {
        position: 'top-right',
        style: {
          background: 'var(--color-bg-primary)',
          color: 'var(--color-text-primary)',
        },
      }
    );
  },
};
