// frontend/src/components/Sidebar.tsx
// 可折叠侧栏容器：左侧 fixed 面板，承载 SessionList；折叠态 w-0 隐藏，展开态 w-[260px]。

import { useEffect, useRef } from 'react';
import type { SessionMeta } from '@/types/session';
import { cn } from '@/lib/cn';
import { SessionList } from './SessionList';

interface Props {
  open: boolean;
  mobile?: boolean;
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
  mobile = false,
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
  const sidebarRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (open && mobile) sidebarRef.current?.querySelector<HTMLButtonElement>('button')?.focus();
  }, [open, mobile]);
  if (!open) return null;

  return (
    <aside
      id="workspace-sidebar"
      ref={sidebarRef}
      aria-label="会话侧栏"
      onKeyDown={(event) => {
        if (!mobile || event.key !== 'Tab') return;
        const items = sidebarRef.current?.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), a[href], [tabindex="0"]');
        if (!items?.length) return;
        const first = items[0]; const last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }}
      className={cn(
        'fixed top-0 left-0 z-40 h-dvh w-[260px] max-w-[calc(100vw-40px)]',
        'bg-ink-900/95 backdrop-blur-md',
        'border-r border-ink-700',
        'shadow-[16px_0_48px_-24px_rgba(0,0,0,0.5)]',
      )}
    >
      <div className="relative flex h-full min-h-0 w-full flex-col">
        <div className="flex h-[76px] shrink-0 items-center justify-between border-b border-ink-700/70 px-4">
          <div><p className="font-display text-lg text-ink-100">工作空间<span className="ml-2 text-amber">/</span></p><p className="mt-1 font-mono text-[9px] tracking-[0.2em] text-ink-500">SPATIAL WORKSPACE</p></div>
        <button
          type="button"
          onClick={onToggle}
          aria-label="关闭会话侧栏"
          title="关闭会话侧栏"
          className={cn(
            'flex h-8 w-8 items-center justify-center',
            'rounded-lg border border-ink-700 bg-ink-900',
            'font-mono text-[15px] leading-none text-ink-500',
            'transition-colors duration-150 motion-reduce:transition-none',
            'hover:text-ink-200 hover:border-ink-600',
          )}
        >
          ‹
        </button>
        </div>

        {/* SessionList 主体 */}
        <div className="min-h-0 flex-1"><SessionList
          sessions={sessions}
          activeId={activeId}
          loading={loading}
          error={error}
          onActivate={onActivate}
          onRename={onRename}
          onDelete={onDelete}
          onCreate={onCreate}
        /></div>

        {/* 底部 Footer：micro-hint */}
        <div className="border-t border-ink-700/70 px-4 py-3">
          <div className="flex items-center justify-between font-mono text-[9px] tracking-[0.18em] text-ink-500">
            <span>GIS RUNTIME</span><span className="text-amber/70">LOCAL WORKSPACE</span>
          </div>
        </div>
      </div>
    </aside>
  );
}
