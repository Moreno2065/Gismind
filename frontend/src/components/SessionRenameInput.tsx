// frontend/src/components/SessionRenameInput.tsx
// 内联重命名输入框：用于替换 SessionCard 标题区域的展示。
// 行为：Enter / blur 提交（空值撤销），Escape 撤销。

import { useCallback, useEffect, useRef } from 'react';
import { cn } from '@/lib/cn';

interface Props {
  initialTitle: string;
  onCommit: (newTitle: string) => void;
  onCancel: () => void;
}

export function SessionRenameInput({ initialTitle, onCommit, onCancel }: Props) {
  const inputRef = useRef<HTMLInputElement | null>(null);

  // 挂载即聚焦 & 选中全部
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.focus();
    el.select();
  }, []);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const v = e.currentTarget.value.trim();
        if (v.length === 0) {
          onCancel();
        } else {
          onCommit(v);
        }
      } else if (e.key === 'Escape') {
        e.preventDefault();
        onCancel();
      }
    },
    [onCommit, onCancel],
  );

  const handleBlur = useCallback(
    (e: React.FocusEvent<HTMLInputElement>) => {
      const v = e.currentTarget.value.trim();
      if (v.length === 0) {
        onCancel();
      } else {
        onCommit(v);
      }
    },
    [onCommit, onCancel],
  );

  return (
    <input
      ref={inputRef}
      type="text"
      // defaultValue is intentional: the parent only mounts this component once per rename
      // session, so the input never needs to sync external state changes after initial mount.
      defaultValue={initialTitle}
      onKeyDown={handleKeyDown}
      onBlur={handleBlur}
      // 阻止冒泡 —— 避免 blur 时触发外层卡片的 click
      onClick={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
      spellCheck={false}
      autoComplete="off"
      maxLength={120}
      aria-label="Rename session"
      className={cn(
        'block w-full rounded-sm bg-ink-900 px-1.5 py-0.5',
        'font-mono text-[13px] text-ink-100',
        'border border-amber outline-none',
        'placeholder:text-ink-500',
      )}
      placeholder="Untitled dispatch"
    />
  );
}
