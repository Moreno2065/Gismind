// frontend/src/hooks/useTheme.ts
// 主题状态管理（dark / light + .light class + localStorage）。
// 默认 dark（保留既有 cartographic intelligence 视觉）；
// 通过 <html> 上的 .light class 驱动 :root.light 变量覆盖。

import { useCallback, useEffect, useState } from 'react';

export type Theme = 'dark' | 'light';

const STORAGE_KEY = 'gismind.theme';
const LIGHT_CLASS = 'light';

function readInitialTheme(): Theme {
  if (typeof window === 'undefined') return 'dark';
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === 'dark' || raw === 'light') return raw;
  } catch {
    /* storage 不可用 — 静默 */
  }
  return 'dark';
}

function applyThemeClass(theme: Theme) {
  if (typeof document === 'undefined') return;
  const html = document.documentElement;
  if (theme === 'light') {
    html.classList.add(LIGHT_CLASS);
  } else {
    html.classList.remove(LIGHT_CLASS);
  }
}

export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(readInitialTheme);

  // 初始化时同步 DOM class，避免首帧闪烁
  useEffect(() => {
    applyThemeClass(theme);
  }, [theme]);

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* noop */
    }
  }, []);

  const toggle = useCallback(() => {
    setThemeState((prev) => {
      const next: Theme = prev === 'dark' ? 'light' : 'dark';
      try {
        window.localStorage.setItem(STORAGE_KEY, next);
      } catch {
        /* noop */
      }
      return next;
    });
  }, []);

  return { theme, setTheme, toggle };
}
