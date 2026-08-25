// frontend/src/components/SessionCard.tsx
// 单个 Session 的 "Field Dispatch" 卡。
// 序列号 STM-XXXX 来自 id 末 4 位 hex；hover 出现 ✎ / × 操作；active 态有左侧 2px amber 条。

import { useCallback, useState } from 'react';
import type { SessionMeta } from '@/types/session';
import { cn } from '@/lib/cn';
import { SessionRenameInput } from './SessionRenameInput';

interface Props {
  meta: SessionMeta;
  active: boolean;
  onActivate: () => void;
  onRename: (newTitle: string) => void;
  onDelete: () => void;
}

/** 从 session id 末 4 位 hex 推导出显示用序列号 STM-XXXX。 */
function dispatchSerial(id: string): string {
  const tail = (id.replace(/[^a-f0-9]/gi, '').slice(-4) || '0000')
    .toUpperCase()
    .padStart(4, '0');
  return `STM-${tail}`;
}

/** 相对时间格式化：< 1m "just now"，< 1h "Xm"，< 24h "Xh"，< 30d "Xd"，否则 yyyy-mm-dd。 */
function formatRelativeTime(ms: number, now: number = Date.now()): string {
  const delta = now - ms;
  const sec = Math.floor(delta / 1000);
  if (sec < 60) return 'just now';
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day}d`;
  return new Date(ms).toISOString().slice(0, 10);
}

export function SessionCard({ meta, active, onActivate, onRename, onDelete }: Props) {
  const [renaming, setRenaming] = useState(false);
  const serial = dispatchSerial(meta.id);
  const rel = formatRelativeTime(meta.updated_at);

  const handleRenameClick = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      setRenaming(true);
    },
    [],
  );

  const handleDeleteClick = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      const ok = window.confirm(`Delete dispatch ${serial}?`);
      if (ok) onDelete();
    },
    [onDelete, serial],
  );

  const handleCommit = useCallback(
    (newTitle: string) => {
      setRenaming(false);
      onRename(newTitle);
    },
    [onRename],
  );

  const handleCancel = useCallback(() => {
    setRenaming(false);
  }, []);

  const handleActivateKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        onActivate();
      }
    },
    [onActivate],
  );

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={renaming ? undefined : onActivate}
      onKeyDown={renaming ? undefined : handleActivateKeyDown}
      className={cn(
        'group relative cursor-pointer select-none',
        'rounded-sm border bg-ink-800/60 px-3 py-2',
        'transition-colors duration-150',
        active
          ? 'border-l-2 border-l-amber border-r border-y-ink-700 bg-amber/5'
          : 'border-ink-700 hover:border-amber/40',
      )}
    >
      {/* 顶部：序列号 + has_map 标（tool_count 移到下方 meta strip，避免与 hover 按钮重叠） */}
      <div className="mb-1 flex items-center justify-between font-mono text-[10px] leading-none">
        <span
          className={cn(
            'uppercase tracking-[0.18em]',
            active ? 'text-amber' : 'text-ink-500',
          )}
        >
          {serial}
        </span>
        <span className="flex items-center gap-2 text-ink-500">
          {meta.has_map && (
            <span
              className={cn(active ? 'text-amber' : 'text-ink-500')}
              aria-label="has map"
              title="contains a map layer"
            >
              ⌖
            </span>
          )}
        </span>
      </div>

      {/* 标题 / 重命名输入 */}
      <div className="min-h-[18px]">
        {renaming ? (
          <SessionRenameInput
            initialTitle={meta.title}
            onCommit={handleCommit}
            onCancel={handleCancel}
          />
        ) : (
          <div
            className={cn(
              'truncate font-mono text-[13px] leading-[18px]',
              'text-ink-100 group-hover:text-ink-50',
            )}
            title={meta.title}
          >
            {meta.title || 'Untitled dispatch'}
          </div>
        )}
      </div>

      {/* hairline 分隔 */}
      <div className="mt-2 border-t border-ink-700" />

      {/* meta strip */}
      <div className="mt-1.5 flex items-center justify-between font-mono text-[9px] leading-none text-ink-500">
        <span className="uppercase tracking-[0.18em]">
          ⌗ {meta.tool_count} tools
        </span>
        <span className="uppercase tracking-[0.18em]" title={new Date(meta.updated_at).toISOString()}>
          ⟲ {rel}
        </span>
      </div>

      {/* hover-only 操作图标 */}
      {!renaming && (
        <>
          <button
            type="button"
            onClick={handleRenameClick}
            aria-label="Rename dispatch"
            title="Rename"
            className={cn(
              'absolute right-7 top-1.5',
              'h-5 w-5 rounded-sm',
              'flex items-center justify-center',
              'font-mono text-[12px] text-ink-500',
              'opacity-0 transition-opacity duration-150',
              'hover:bg-ink-900 hover:text-amber',
              'group-hover:opacity-100',
            )}
          >
            ✎
          </button>
          <button
            type="button"
            onClick={handleDeleteClick}
            aria-label="Delete dispatch"
            title="Delete"
            className={cn(
              'absolute right-2 top-1.5',
              'h-5 w-5 rounded-sm',
              'flex items-center justify-center',
              'font-mono text-[12px] text-ink-500',
              'opacity-0 transition-opacity duration-150',
              'hover:bg-ink-900 hover:text-signal-error',
              'group-hover:opacity-100',
            )}
          >
            ×
          </button>
        </>
      )}
    </div>
  );
}
