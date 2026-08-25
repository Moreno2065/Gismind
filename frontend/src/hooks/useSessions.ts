// frontend/src/hooks/useSessions.ts
// 会话列表与当前激活会话管理。负责与 /api/sessions 交互、localStorage 持久化
// 以及对 ChatPanel 消息缓存（localStorage）的协调清理。

import { useCallback, useEffect, useRef, useState } from 'react';
import type { SessionMeta } from '@/types/session';
import {
  createSession as apiCreateSession,
  deleteSession as apiDeleteSession,
  getActiveSessionId,
  listSessions as apiListSessions,
  renameSession as apiRenameSession,
  setActiveSessionId,
} from '@/api/client';

const MSG_KEY_PREFIX = 'gismind.messages.';
const LEGACY_MSG_KEY = 'gismind.messages';

/** 把 sessions 按 updated_at 降序排序（新的在前）。 */
function sortByUpdatedAt(items: SessionMeta[]): SessionMeta[] {
  return [...items].sort((a, b) => b.updated_at - a.updated_at);
}

/** 兜底清理 ChatPanel 使用的消息缓存。 */
function clearMessageCache(id: string): void {
  try {
    localStorage.removeItem(MSG_KEY_PREFIX + id);
  } catch {
    /* noop */
  }
  try {
    const legacy = localStorage.getItem(LEGACY_MSG_KEY);
    if (legacy) {
      // 旧版单一 key 的兜底：仅当旧值与被删除 session 匹配时清理
      try {
        const parsed = JSON.parse(legacy) as { sessionId?: string };
        if (parsed?.sessionId === id) {
          localStorage.removeItem(LEGACY_MSG_KEY);
        }
      } catch {
        // 非 JSON 字符串 —— 仅当值等于 id 时清理
        if (legacy === id) {
          localStorage.removeItem(LEGACY_MSG_KEY);
        }
      }
    }
  } catch {
    /* noop */
  }
}

export interface UseSessionsApi {
  sessions: SessionMeta[];               // 按 updated_at 降序
  activeId: string | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  create: () => Promise<string>;          // 创建新会话并自动切到它
  rename: (id: string, title: string) => Promise<void>;
  remove: (id: string) => Promise<void>;  // 删除（cascade 内存消息缓存）
  activate: (id: string) => void;         // 切换 activeId（不拉 messages）
}

export function useSessions(): UseSessionsApi {
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [activeId, setActiveIdState] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 一个挂载期间统一的 AbortController；unmount 时取消。
  const controllerRef = useRef<AbortController | null>(null);

  // 持有最新 state 的 ref —— 因为 refresh() 内部需要参考当前 sessions / activeId
  // 但 useCallback 会把初始值锁死。
  const stateRef = useRef({ sessions, activeId });
  stateRef.current = { sessions, activeId };

  const refresh = useCallback(async (): Promise<void> => {
    const ctrl = controllerRef.current;
    if (!ctrl) return;
    setLoading(true);
    try {
      const items = await apiListSessions(ctrl.signal);
      const sorted = sortByUpdatedAt(items);
      setSessions(sorted);
      setError(null);

      // 若当前 activeId 已不在列表中，回退到列表头部或清空
      const cur = stateRef.current.activeId;
      if (cur && !sorted.some((s) => s.id === cur)) {
        const fallback = sorted[0]?.id ?? null;
        setActiveIdState(fallback);
        if (fallback) {
          setActiveSessionId(fallback);
        } else {
          try {
            localStorage.removeItem('gismind.active_session');
          } catch {
            /* noop */
          }
        }
      } else if (!cur && sorted.length > 0) {
        const fallback = sorted[0].id;
        setActiveIdState(fallback);
        setActiveSessionId(fallback);
      }
    } catch (e) {
      // AbortError 静默忽略
      if (e instanceof Error && e.name === 'AbortError') return;
      setError(e instanceof Error ? e.message : String(e));
      // 失败时 sessions 保留旧值
    } finally {
      setLoading(false);
    }
  }, []);

  const create = useCallback(async (): Promise<string> => {
    const ctrl = controllerRef.current;
    if (!ctrl) throw new Error('useSessions: unmounted');
    try {
      const created = await apiCreateSession(ctrl.signal);
      setActiveSessionId(created.id);
      setActiveIdState(created.id);
      setError(null);
      // 拉最新列表（含新 session）
      await refresh();
      return created.id;
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') {
        throw new Error('useSessions: aborted');
      }
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      throw e;
    }
  }, [refresh]);

  const rename = useCallback(async (id: string, title: string): Promise<void> => {
    const ctrl = controllerRef.current;
    if (!ctrl) return;
    try {
      await apiRenameSession(id, title, ctrl.signal);
      // 乐观更新：本地先把 title 改了
      setSessions((prev) =>
        prev.map((s) => (s.id === id ? { ...s, title } : s)),
      );
      setError(null);
      // 再拉一次真实状态（updated_at 也会变）
      void refresh();
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') return;
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      // 404 等错误透传上层处理
      throw e;
    }
  }, [refresh]);

  const remove = useCallback(async (id: string): Promise<void> => {
    const ctrl = controllerRef.current;
    if (!ctrl) return;
    // 记录删除前的 activeId 用于决定是否要迁移
    const wasActive = stateRef.current.activeId === id;
    try {
      await apiDeleteSession(id, ctrl.signal);
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') return;
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      throw e;
    }

    // 内存数组中过滤掉该 id，并按 updated_at 重排
    const remaining = sortByUpdatedAt(
      stateRef.current.sessions.filter((s) => s.id !== id),
    );
    setSessions(remaining);

    // 清理该 session 的本地消息缓存
    clearMessageCache(id);

    if (wasActive) {
      if (remaining.length > 0) {
        const nextActive = remaining[0].id;
        setActiveIdState(nextActive);
        setActiveSessionId(nextActive);
      } else {
        // 列表空 —— 自动建一个
        try {
          const created = await apiCreateSession(ctrl.signal);
          setActiveIdState(created.id);
          setActiveSessionId(created.id);
          // 拉一次以同步 sessions
          void refresh();
        } catch (e2) {
          if (e2 instanceof Error && e2.name === 'AbortError') return;
          const msg = e2 instanceof Error ? e2.message : String(e2);
          setError(msg);
        }
      }
    }
  }, [refresh]);

  const activate = useCallback((id: string): void => {
    setActiveSessionId(id);
    setActiveIdState(id);
  }, []);

  // 初始化：建 controller + 首屏 load
  useEffect(() => {
    const ctrl = new AbortController();
    controllerRef.current = ctrl;

    (async () => {
      setLoading(true);
      try {
        const items = await apiListSessions(ctrl.signal);
        const sorted = sortByUpdatedAt(items);
        setSessions(sorted);

        const stored = getActiveSessionId();
        if (stored && sorted.some((s) => s.id === stored)) {
          setActiveIdState(stored);
        } else if (sorted.length > 0) {
          // 没有 activeId 但有列表 —— 默认指向列表头
          const fallback = sorted[0].id;
          setActiveIdState(fallback);
          setActiveSessionId(fallback);
        } else {
          // 完全没有 —— 自动建一个
          try {
            const created = await apiCreateSession(ctrl.signal);
            setActiveIdState(created.id);
            setActiveSessionId(created.id);
            const items2 = await apiListSessions(ctrl.signal);
            setSessions(sortByUpdatedAt(items2));
          } catch (e) {
            if (e instanceof Error && e.name === 'AbortError') return;
            setError(e instanceof Error ? e.message : String(e));
          }
        }
        setError(null);
      } catch (e) {
        if (e instanceof Error && e.name === 'AbortError') return;
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    })();

    return () => {
      ctrl.abort();
      controllerRef.current = null;
    };
  }, []);

  return {
    sessions,
    activeId,
    loading,
    error,
    refresh,
    create,
    rename,
    remove,
    activate,
  };
}
