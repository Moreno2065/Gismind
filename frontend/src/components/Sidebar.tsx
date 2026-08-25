// frontend/src/components/Sidebar.tsx
// 可折叠侧栏容器：左侧 fixed 面板，承载 SessionList；折叠态 w-0 隐藏，展开态 w-[260px]。

import { useCallback } from 'react';
import type { SessionMeta } from '@/types/session';
import { cn } from '@/lib/cn';
import { SessionList } from './SessionList';

interface Props {
  open: boolean;
  onToggle: () => void;
  sessions: SessionMeta[];
  activeId: string | null;
  loading: boolean;
  error: string | null;
  onActivate: (id: string) => void;
  onRename: (id: string, newTitle: string) => void;
  onDelete: (id: string) => void;
  onCreate: () => void;
}

export function Sidebar({
  open,
  onToggle,
  sessions,
  activeId,
  loading,
  error,
  onActivate,
  onRename,
  onDelete,
  onCreate,
}: Props) {
  const handleToggle = useCallback(() => {
    onToggle();
  }, [onToggle]);

  return (
    <aside
      aria-hidden={!open}
      className={cn(
        'fixed top-0 left-0 z-30 h-screen',
        'bg-ink-900/95 backdrop-blur-md',
        'border-r border-ink-700',
        'transition-[width] duration-200 ease-out',
        open ? 'w-[260px]' : 'w-0 overflow-hidden',
      )}
    >
      <div className="relative flex h-full w-[260px] flex-col">
        {/* 折叠按钮：右边缘垂直居中 */}
        <button
          type="button"
          onClick={handleToggle}
          aria-label="Collapse sidebar"
          title="Collapse"
          className={cn(
            'absolute top-1/2 -right-4 z-10 -translate-y-1/2',
            'flex h-8 w-8 items-center justify-center',
            'rounded-sm border border-ink-700 bg-ink-900',
            'font-mono text-[15px] leading-none text-ink-500',
            'transition-colors duration-150',
            'hover:text-ink-200 hover:border-ink-600',
          )}
        >
          ‹
        </button>

        {/* SessionList 主体 */}
        <SessionList
          sessions={sessions}
          activeId={activeId}
          loading={loading}
          error={error}
          onActivate={onActivate}
          onRename={onRename}
          onDelete={onDelete}
          onCreate={onCreate}
        />

        {/* 底部 Footer：micro-hint */}
        <div className="border-t border-ink-700 px-3 py-2">
          <div className="flex items-center justify-end font-mono text-[9px] uppercase tracking-[0.18em] text-ink-500">
            <span title="Cmd/Ctrl + N">⏎ ⌘ + N → new</span>
          </div>
        </div>
      </div>
    </aside>
  );
}
