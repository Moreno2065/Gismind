// frontend/src/components/ErrorBoundary.tsx
// React 错误边界 — 捕获渲染异常，避免白屏。
// 支持嵌套使用：外层为全局兜底，内层为各区域独立隔离（如 ChatPanel、Sidebar、Header）。

import { Component, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
  /** Custom fallback UI — when omitted, renders a full-screen "refresh page" fallback. */
  fallback?: ReactNode;
  /** Title shown in the default fallback. */
  title?: string;
}

interface State {
  hasError: boolean;
  error?: Error;
}

export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error('ErrorBoundary caught error:', error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;
      return (
        <div className="flex h-full flex-col items-center justify-center p-6 text-center">
          <h1 className="font-display text-xl text-signal-error">
            {this.props.title || '应用发生错误'}
          </h1>
          <p className="mt-3 max-w-md text-sm text-ink-400">
            {this.state.error?.message || '未知错误'}
          </p>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="mt-6 rounded-lg border border-ink-600 bg-ink-800 px-4 py-2 text-sm text-ink-200 transition hover:border-amber hover:text-amber"
          >
            刷新页面
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
