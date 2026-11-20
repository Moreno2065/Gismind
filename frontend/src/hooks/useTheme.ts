// frontend/src/hooks/useTheme.ts
// 主题状态管理（dark / light + <html> class + localStorage + 多组件同步）。
// 默认 dark（保留既有 cartographic intelligence 视觉）；
// 通过 <html> 上的 .dark / .light class 互斥切换变量覆盖。

import { useCallback, useEffect, useState } from 'react';

export type Theme = 'dark' | 'light';

const STORAGE_KEY = 'gismind.theme';
const DARK_CLASS = 'dark';
const LIGHT_CLASS = 'light';

const themeListeners = new Set<(theme: Theme) => void>();

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
    html.classList.remove(DARK_CLASS);
    html.classList.add(LIGHT_CLASS);
  } else {
    html.classList.remove(LIGHT_CLASS);
    html.classList.add(DARK_CLASS);
  }
}

function broadcastTheme(next: Theme) {
  applyThemeClass(next);
  try {
    window.localStorage.setItem(STORAGE_KEY, next);
  } catch {
    /* noop */
  }
  themeListeners.forEach((listener) => listener(next));
}

export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(readInitialTheme);

  // 初始化与订阅：保证多个 useTheme() 调用方（如 App 与 ChartView）实时同步
  useEffect(() => {
    applyThemeClass(theme);
  }, [theme]);

  useEffect(() => {
    const listener = (next: Theme) => setThemeState(next);
    themeListeners.add(listener);
    const onStorage = (event: StorageEvent) => {
      if (event.key !== STORAGE_KEY) return;
      const next: Theme = event.newValue === 'light' ? 'light' : 'dark';
      applyThemeClass(next);
      setThemeState(next);
    };
    window.addEventListener('storage', onStorage);
    return () => {
      themeListeners.delete(listener);
      window.removeEventListener('storage', onStorage);
    };
  }, []);

  const setTheme = useCallback((next: Theme) => {
    broadcastTheme(next);
  }, []);

  const toggle = useCallback(() => {
    const next: Theme = readInitialTheme() === 'dark' ? 'light' : 'dark';
    broadcastTheme(next);
  }, []);

  return { theme, setTheme, toggle };
}
