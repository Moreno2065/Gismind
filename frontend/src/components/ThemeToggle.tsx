// frontend/src/components/ThemeToggle.tsx
// 主题切换控件 — Segmented Control 风格。
// 顶栏上 dark / light 双段选择；active 段用 amber + 细描边，其他 muted。
// 设计参考：测量仪器般的精度感（mono 字体 / 细线 / 边框 ridge）。

import { useCallback, useEffect } from 'react';
import type { Theme } from '@/hooks/useTheme';
import { cn } from '@/lib/cn';

interface ThemeToggleProps {
  theme: Theme;
  onChange: (t: Theme) => void;
}

export function ThemeToggle({ theme, onChange }: ThemeToggleProps) {
  // 键盘快捷键：按 t 键切主题（输入框聚焦时不响应，避免输入字符 't'）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 't' || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      const tag = (e.target as HTMLElement | null)?.tagName?.toLowerCase();
      if (tag === 'input' || tag === 'textarea' || (e.target as HTMLElement | null)?.isContentEditable) return;
      e.preventDefault();
      onChange(theme === 'dark' ? 'light' : 'dark');
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [theme, onChange]);

  const handlePick = useCallback(
    (next: Theme) => () => onChange(next),
    [onChange],
  );

  return (
    <div
      className="flex select-none items-center gap-2 rounded-md border border-ink-700 bg-ink-900/60 p-1 backdrop-blur-md shadow-bubble"
      role="radiogroup"
      aria-label="主题模式"
    >
      <Segment
        active={theme === 'dark'}
        label="暗"
        hint="DARK"
        onClick={handlePick('dark')}
      >
        <MoonIcon />
      </Segment>
      <div className="h-4 w-px bg-ink-700" aria-hidden="true" />
      <Segment
        active={theme === 'light'}
        label="亮"
        hint="LIGHT"
        onClick={handlePick('light')}
      >
        <SunIcon />
      </Segment>
    </div>
  );
}

interface SegmentProps {
  active: boolean;
  label: string;
  hint: string;
  onClick: () => void;
  children: React.ReactNode;
}

function Segment({ active, label, hint, onClick, children }: SegmentProps) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={active}
      onClick={onClick}
      title={`切换至 ${hint} 主题（键盘 t）`}
      className={cn(
        'group relative flex items-center gap-1.5 rounded-sm px-2 py-1 font-mono text-[10px] uppercase tracking-[0.18em] transition',
        active
          ? 'bg-amber/10 text-amber shadow-[0_0_0_1px_rgb(var(--c-amber)/0.45),0_0_10px_-2px_rgb(var(--c-amber)/0.5)]'
          : 'text-ink-400 hover:bg-ink-800/60 hover:text-ink-200',
      )}
    >
      <span className={cn('flex h-3.5 w-3.5 items-center justify-center', active && 'animate-theme-icon-in')}>
        {children}
      </span>
      <span className="leading-none">{active ? label : hint}</span>
      {active && (
        <span className="ml-0.5 inline-block h-1 w-1 rounded-full bg-amber shadow-glow" aria-hidden="true" />
      )}
    </button>
  );
}

// ---------------------------------------------------------------------------
// 图标 — 自绘细线 SVG，保持与 BrandMark 的笔触一致
// ---------------------------------------------------------------------------

function MoonIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M13.5 9.5A5.5 5.5 0 1 1 6.5 2.5a4.5 4.5 0 0 0 7 7Z"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill={undefined}
      />
    </svg>
  );
}

function SunIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <circle cx="8" cy="8" r="2.8" stroke="currentColor" strokeWidth="1.4" />
      <g stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
        <line x1="8" y1="1.5" x2="8" y2="3" />
        <line x1="8" y1="13" x2="8" y2="14.5" />
        <line x1="1.5" y1="8" x2="3" y2="8" />
        <line x1="13" y1="8" x2="14.5" y2="8" />
        <line x1="3.2" y1="3.2" x2="4.3" y2="4.3" />
        <line x1="11.7" y1="11.7" x2="12.8" y2="12.8" />
        <line x1="3.2" y1="12.8" x2="4.3" y2="11.7" />
        <line x1="11.7" y1="4.3" x2="12.8" y2="3.2" />
      </g>
    </svg>
  );
}
