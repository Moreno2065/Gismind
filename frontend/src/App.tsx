// frontend/src/App.tsx
// Gismind — ChatGPT 风格单页对话流。
// 顶栏（品牌 + 主题切换 + session）+ 可折叠侧栏（会话列表）+ 主对话区（ChatPanel）。
// 主题切换通过 :root/.light 切换 ink/amber/signal 等所有 token（见 index.css）。

import { useEffect, useRef, useState } from 'react';
import { ChatPanel } from '@/components/ChatPanel';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { Sidebar } from '@/components/Sidebar';
import { ThemeToggle } from '@/components/ThemeToggle';
import { useTheme } from '@/hooks/useTheme';
import { useSessions } from '@/hooks/useSessions';
import { getHealth, type HealthResponse } from '@/api/client';

const SIDEBAR_OPEN_KEY = 'gismind.sidebar_open';

export default function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const { theme, setTheme } = useTheme();
  const sessionsApi = useSessions();
  const [narrow, setNarrow] = useState(() => window.innerWidth < 768);
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(() => readSidebarOpen());
  const toggleRef = useRef<HTMLButtonElement>(null);
  const workspaceRef = useRef<HTMLDivElement>(null);
  const closeSidebar = () => {
    setSidebarOpen(false);
    window.requestAnimationFrame(() => toggleRef.current?.focus());
  };

  useEffect(() => {
    const resize = () => {
      const next = window.innerWidth < 768;
      if (next !== narrow) { setNarrow(next); setSidebarOpen(next ? false : readSidebarOpen()); }
    };
    window.addEventListener('resize', resize);
    return () => window.removeEventListener('resize', resize);
  }, [narrow]);

  useEffect(() => {
    if (workspaceRef.current) workspaceRef.current.inert = narrow && sidebarOpen;
    if (!narrow || !sidebarOpen) return;
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') closeSidebar(); };
    window.addEventListener('keydown', escape);
    return () => window.removeEventListener('keydown', escape);
  }, [narrow, sidebarOpen]);

  // 持久化侧栏状态
  useEffect(() => {
    if (narrow) return;
    try {
      localStorage.setItem(SIDEBAR_OPEN_KEY, sidebarOpen ? '1' : '0');
    } catch {
      /* noop */
    }
  }, [sidebarOpen, narrow]);

  useEffect(() => {
    let ac = new AbortController();
    const check = () => {
      getHealth(ac.signal)
        .then(setHealth)
        .catch(() => setHealth(null));
    };
    check();
    const t = setInterval(check, 30_000);
    return () => {
      ac.abort();
      clearInterval(t);
    };
  }, []);

  return (
    <ErrorBoundary>
      <div className="relative flex h-dvh overflow-hidden bg-ink-950 text-ink-200">
        {/* 背景大气层 — 地形网格 + 颗粒 + 顶部辉光 */}
        <div className="pointer-events-none absolute inset-0 z-0">
          <TopoGrid />
          <GrainOverlay />
          <div className="absolute -top-40 left-1/2 h-80 w-[60rem] -translate-x-1/2 rounded-full bg-amber/5 blur-[120px]" />
        </div>

        {/* 侧栏 */}
        {narrow && sidebarOpen && <button type="button" tabIndex={-1} aria-label="关闭会话侧栏遮罩" onClick={closeSidebar} className="fixed inset-0 z-30 bg-ink-950/70 backdrop-blur-sm" />}
        <ErrorBoundary
          title="侧栏发生错误"
          fallback={
            <div className="flex h-full w-64 items-center justify-center border-r border-ink-800 bg-ink-950 p-4">
              <span className="font-mono text-xs text-signal-error">侧栏加载失败</span>
            </div>
          }
        >
          <Sidebar
            open={sidebarOpen}
            mobile={narrow}
            onToggle={closeSidebar}
            sessions={sessionsApi.sessions}
            activeId={sessionsApi.activeId}
            loading={sessionsApi.loading}
            error={sessionsApi.error}
            onActivate={(id) => { sessionsApi.activate(id); if (narrow) closeSidebar(); }}
            onRename={sessionsApi.rename}
            onDelete={sessionsApi.remove}
            onCreate={() => { void sessionsApi.create(); if (narrow) closeSidebar(); }}
          />
        </ErrorBoundary>

        {/* 主内容区（顶栏 + 主对话） */}
        <div ref={workspaceRef} aria-hidden={narrow && sidebarOpen ? true : undefined} style={{ marginLeft: sidebarOpen && !narrow ? 260 : 0 }} className="relative z-10 flex min-w-0 flex-1 flex-col transition-[margin] duration-200 motion-reduce:transition-none">

          {/* 顶栏 */}
          <ErrorBoundary
            title="顶栏发生错误"
            fallback={
              <header className="flex items-center justify-between border-b border-ink-800 bg-ink-950/60 px-4 py-3 backdrop-blur-md">
                <span className="font-display text-base text-ink-100">Gismind</span>
              </header>
            }
          >
            <header className="flex h-[76px] shrink-0 items-center justify-between gap-3 border-b border-ink-700/70 bg-ink-950/80 px-3 backdrop-blur-md sm:px-6">
              <div className="flex shrink-0 items-center gap-2 sm:gap-3">
                <button ref={toggleRef} type="button" aria-label={sidebarOpen ? '收起侧栏' : '展开侧栏'} aria-expanded={sidebarOpen} aria-controls="workspace-sidebar" onClick={() => setSidebarOpen((value) => !value)} className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-ink-700 text-ink-400 transition-colors hover:border-amber/50 hover:text-amber">
                  <svg aria-hidden="true" width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.3"><rect x="2" y="3" width="16" height="14" rx="2" /><path d="M7 3v14" /></svg>
                </button>
                <BrandMark />
                <div className="flex shrink-0 flex-col gap-1.5 whitespace-nowrap">
                  <span className="font-display text-xl leading-none tracking-tight text-ink-100">
                    Gismind
                  </span>
                  <span className="font-mono text-[9px] leading-none tracking-[0.23em] text-amber/80">
                    GIS RUNTIME
                  </span>
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-2 sm:gap-4">
                <ThemeToggle theme={theme} onChange={setTheme} />
                <HealthPill health={health} />
              </div>
            </header>
          </ErrorBoundary>

          {/* 主对话区 */}
          <main className="min-h-0 flex-1 overflow-hidden">
            <ErrorBoundary
              title="对话区发生错误"
              fallback={
                <div className="flex h-full items-center justify-center font-mono text-xs text-signal-error">
                  对话面板加载失败，请刷新页面
                </div>
              }
            >
              {sessionsApi.activeId ? (
                <ChatPanel sessionId={sessionsApi.activeId} onSessionUpdate={sessionsApi.refresh} />
              ) : (
                <div className="flex h-full items-center justify-center text-sm text-ink-400">
                  {sessionsApi.error ? '会话加载失败，请刷新后重试' : '正在准备你的 GIS 工作台…'}
                </div>
              )}
            </ErrorBoundary>
          </main>
        </div>
      </div>
    </ErrorBoundary>
  );
}

function readSidebarOpen(): boolean {
  if (window.innerWidth < 768) return false;
  try {
    const v = localStorage.getItem(SIDEBAR_OPEN_KEY);
    if (v === '0') return false;
    return true;
  } catch {
    return true;
  }
}

function BrandMark() {
  return (
    <div className="relative hidden h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-amber/30 bg-ink-900 text-amber sm:flex">
      <svg width="20" height="20" viewBox="0 0 32 32" fill="none">
        <circle cx="16" cy="14" r="9" stroke="currentColor" strokeWidth="1.5" />
        <path d="M16 2v6m0 12v6M4 14h6m12 0h6" stroke="currentColor" strokeWidth="1" />
        <path d="m12 18 4-10 4 10-4-2-4 2Z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
      </svg>
    </div>
  );
}

function HealthPill({ health }: { health: HealthResponse | null }) {
  if (!health) {
    return (
      <div role="status" aria-label="正在连接服务" title="正在连接服务" className="flex h-7 w-7 shrink-0 items-center justify-center gap-2 rounded-full border border-ink-700 bg-ink-900/60 sm:h-auto sm:w-auto sm:px-3 sm:py-1">
        <span className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-ink-500" />
        <span className="hidden font-mono text-[10px] text-ink-400 sm:inline">连接中</span>
      </div>
    );
  }
  const ok = health.status === 'ok';
  return (
    <div
      role="status"
      aria-label={ok ? '运行正常' : '服务受限'}
      className={`flex h-7 w-7 shrink-0 items-center justify-center gap-2 rounded-full border sm:h-auto sm:w-auto sm:px-3 sm:py-1 ${
        ok ? 'border-signal-done/30 bg-signal-done/5' : 'border-signal-error/30 bg-signal-error/5'
      }`}
      title={(ok ? '运行正常' : '服务受限') + ' · v' + health.version + ' · ' + JSON.stringify(health.checks)}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${ok ? 'bg-signal-done' : 'bg-signal-error'}`} />
      <span className={`hidden whitespace-nowrap font-mono text-[10px] sm:inline ${ok ? 'text-signal-done' : 'text-signal-error'}`}>
        {ok ? '运行正常' : '服务受限'}<span className="hidden sm:inline"> · v{health.version}</span>
      </span>
    </div>
  );
}

/** 地形网格背景 — 在浅色主题下用更深的线条以维持可见度 */
function TopoGrid() {
  return (
    <div
      className="absolute inset-0 opacity-[0.04]"
      style={{
        backgroundImage:
          'linear-gradient(rgb(var(--c-ink-100)) 1px, transparent 1px), linear-gradient(90deg, rgb(var(--c-ink-100)) 1px, transparent 1px)',
        backgroundSize: '48px 48px',
        maskImage: 'radial-gradient(ellipse at 50% 30%, #000 30%, transparent 80%)',
      }}
    />
  );
}

/** 颗粒覆盖层 — 增加质感深度 */
function GrainOverlay() {
  return (
    <div
      className="absolute inset-0 opacity-[0.025] mix-blend-overlay"
      style={{
        backgroundImage:
          "url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/></filter><rect width='100%25' height='100%25' filter='url(%23n)'/></svg>\")",
      }}
    />
  );
}
