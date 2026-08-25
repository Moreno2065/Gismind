// frontend/src/components/SessionList.tsx
// Sessions 列表面板：顶部 mono 标签 + 计数，中间 SessionCard 列表，底部 + NEW DISPATCH 按钮。

import type { SessionMeta } from '@/types/session';
import { cn } from '@/lib/cn';
import { SessionCard } from './SessionCard';

interface Props {
  sessions: SessionMeta[];
  activeId: string | null;
  loading: boolean;
  error: string | null;
  onActivate: (id: string) => void;
  onRename: (id: string, newTitle: string) => void;
  onDelete: (id: string) => void;
  onCreate: () => void;
}

export function SessionList({
  sessions,
  activeId,
  loading,
  error,
  onActivate,
  onRename,
  onDelete,
  onCreate,
}: Props) {
  const archivedCount = 0; // 当前后端不分 active/archived，预留位
  const activeCount = sessions.length;

  return (
    <div className="flex h-full flex-col">
      {/* 顶部：title + 计数 */}
      <div className="px-3 pt-3">
        <div className="flex items-baseline justify-between">
          <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-ink-300">
            GISMIND · SESSIONS
          </span>
        </div>
        <div className="mt-1 font-mono text-[9px] uppercase tracking-[0.18em] text-ink-500">
          {activeCount === 0
            ? '空会话'
            : `${activeCount} active · ${archivedCount} archived`}
        </div>
      </div>

      {/* 顶部 hairline */}
      <div className="mt-3 border-t border-ink-700" />

      {/* 主体：状态 / 列表 */}
      <div className="flex-1 overflow-y-auto px-3 py-3">
        {loading && sessions.length === 0 ? (
          <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-ink-500">
            loading...
          </div>
        ) : error ? (
          <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-signal-error">
            error: {error}
          </div>
        ) : sessions.length === 0 ? (
          <div className="font-mono text-[9px] leading-relaxed text-ink-500">
            No dispatches yet. Press ➕ to begin.
          </div>
        ) : (
          <div className="space-y-2">
            {sessions.map((meta) => (
              <SessionCard
                key={meta.id}
                meta={meta}
                active={meta.id === activeId}
                onActivate={() => onActivate(meta.id)}
                onRename={(t) => onRename(meta.id, t)}
                onDelete={() => onDelete(meta.id)}
              />
            ))}
          </div>
        )}
      </div>

      {/* 底部 hairline */}
      <div className="border-t border-ink-700" />

      {/* NEW DISPATCH 按钮 */}
      <div className="px-3 py-3">
        <button
          type="button"
          onClick={onCreate}
          className={cn(
            'w-full rounded-sm',
            'border border-amber/60 text-amber',
            'font-mono text-[10px] uppercase tracking-[0.18em]',
            'px-3 py-2',
            'transition-colors duration-150',
            'hover:bg-amber/10',
          )}
        >
          + NEW DISPATCH
        </button>
      </div>
    </div>
  );
}
