// frontend/src/App.tsx
// Gismind — ChatGPT 风格单页对话流。
// 顶栏（品牌 + 主题切换 + session）+ 可折叠侧栏（会话列表）+ 主对话区（ChatPanel）。
// 主题切换通过 :root/.light 切换 ink/amber/signal 等所有 token（见 index.css）。

import { useEffect, useState } from 'react';
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
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(() => readSidebarOpen());

  // 持久化侧栏状态
  useEffect(() => {
    try {
      localStorage.setItem(SIDEBAR_OPEN_KEY, sidebarOpen ? '1' : '0');
    } catch {
      /* noop */
    }
  }, [sidebarOpen]);

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
      <div className="relative flex h-screen overflow-hidden bg-ink-950 text-ink-200">
        {/* 背景大气层 — 地形网格 + 颗粒 + 顶部辉光 */}
        <div className="pointer-events-none absolute inset-0 z-0">
          <TopoGrid />
          <GrainOverlay />
          <div className="absolute -top-40 left-1/2 h-80 w-[60rem] -translate-x-1/2 rounded-full bg-amber/5 blur-[120px]" />
        </div>

        {/* 侧栏 */}
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
            onToggle={() => setSidebarOpen((v) => !v)}
            sessions={sessionsApi.sessions}
            activeId={sessionsApi.activeId}
            loading={sessionsApi.loading}
            error={sessionsApi.error}
            onActivate={sessionsApi.activate}
            onRename={sessionsApi.rename}
            onDelete={sessionsApi.remove}
            onCreate={sessionsApi.create}
          />
        </ErrorBoundary>

        {/* 主内容区（顶栏 + 主对话） */}
        <div className="relative z-10 flex flex-1 flex-col">
          {/* 折叠态：左侧浮动展开按钮（与侧栏内折叠按钮镜像） */}
          {!sidebarOpen && (
            <button
              type="button"
              onClick={() => setSidebarOpen(true)}
              className="fixed left-2 top-1/2 z-40 -translate-y-1/2 flex h-8 w-8 items-center justify-center rounded-sm border border-ink-700 bg-ink-900 font-mono text-[15px] leading-none text-ink-500 shadow-bubble transition-colors duration-150 hover:text-ink-200 hover:border-ink-600"
              title="展开侧栏"
              aria-label="展开侧栏"
            >
              ›
            </button>
          )}

          {/* 顶栏 */}
          <ErrorBoundary
            title="顶栏发生错误"
            fallback={
              <header className="flex items-center justify-between border-b border-ink-800 bg-ink-950/60 px-4 py-3 backdrop-blur-md">
                <span className="font-display text-base text-ink-100">Gismind</span>
              </header>
            }
          >
            <header className="flex items-center justify-between border-b border-ink-800 bg-ink-950/60 px-4 py-3 backdrop-blur-md">
              <div className="flex items-center gap-3">
                <BrandMark />
                <div className="flex flex-col">
                  <span className="font-display text-base leading-none text-ink-100">
                    Gismind
                  </span>
                  <span className="font-mono text-[10px] leading-none text-ink-500">
                    spatial intelligence agent
                  </span>
                </div>
              </div>
              <div className="flex items-center gap-3">
                <ThemeToggle theme={theme} onChange={setTheme} />
                <HealthPill health={health} />
              </div>
            </header>
          </ErrorBoundary>

          {/* 主对话区 */}
          <main className="flex-1 overflow-hidden">
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
                <div className="flex h-full items-center justify-center font-mono text-[11px] uppercase tracking-[0.22em] text-ink-500">
                  initializing dispatch…
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
    <div className="relative flex h-9 w-9 items-center justify-center rounded-lg border border-amber/30 bg-ink-900 shadow-glow">
      <svg width="20" height="20" viewBox="0 0 32 32" fill="none">
        <circle cx="16" cy="14" r="9" stroke="#ff7a1a" strokeWidth="2.5" />
        <path d="M16 5 L16 14 L22 16" stroke="#ff7a1a" strokeWidth="2.5" strokeLinecap="round" fill="none" />
        <path d="M4 26 Q16 18 28 26" stroke="#5b6776" strokeWidth="1.5" fill="none" strokeLinecap="round" />
      </svg>
    </div>
  );
}

function HealthPill({ health }: { health: HealthResponse | null }) {
  if (!health) {
    return (
      <div className="flex items-center gap-2 rounded-full border border-ink-700 bg-ink-900/60 px-3 py-1">
        <span className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-ink-500" />
        <span className="font-mono text-[10px] text-ink-400">checking…</span>
      </div>
    );
  }
  const ok = health.status === 'ok';
  return (
    <div
      className={`flex items-center gap-2 rounded-full border px-3 py-1 ${
        ok ? 'border-signal-done/30 bg-signal-done/5' : 'border-signal-error/30 bg-signal-error/5'
      }`}
      title={JSON.stringify(health.checks)}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${ok ? 'bg-signal-done' : 'bg-signal-error'}`} />
      <span className={`font-mono text-[10px] ${ok ? 'text-signal-done' : 'text-signal-error'}`}>
        {ok ? 'all systems' : 'degraded'} · v{health.version}
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