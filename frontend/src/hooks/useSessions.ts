// frontend/src/hooks/useSessions.ts
// 会话列表与当前激活会话管理。负责与 /api/sessions 交互、localStorage 持久化
// 以及对 ChatPanel 消息缓存（localStorage）的协调清理。

import { useCallback, useEffect, useRef, useState } from 'react';
import type { SessionMeta } from '@/types/session';
import {
  clearActiveSessionId,
  createSession as apiCreateSession,
  deleteSession as apiDeleteSession,
  getActiveSessionId,
  getSessionMeta as apiGetSessionMeta,
  RuntimeApiError,
  listSessions as apiListSessions,
  renameSession as apiRenameSession,
  setActiveSessionId,
} from '@/api/client';

const MSG_KEY_PREFIX = 'gismind.messages.';

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
  const controllerRef = useRef<AbortController | null>(null);
  const refreshVersion = useRef(0);
  const selectionVersion = useRef(0);
  // Update these refs with each publication, not one render later: mutations
  // can finish before React flushes the preceding selection update.
  const stateRef = useRef({ sessions, activeId });
  const alive = useCallback((ctrl: AbortController) =>
    controllerRef.current === ctrl && !ctrl.signal.aborted, []);

  const publishSessions = useCallback((items: SessionMeta[]) => {
    const sorted = sortByUpdatedAt(items);
    stateRef.current = { ...stateRef.current, sessions: sorted };
    setSessions(sorted);
  }, []);
  const selectSession = useCallback((id: string | null) => {
    stateRef.current = { ...stateRef.current, activeId: id };
    setActiveIdState(id);
    if (id) setActiveSessionId(id);
    else clearActiveSessionId();
  }, []);
  const addCreated = useCallback((created: Awaited<ReturnType<typeof apiCreateSession>>) => {
    publishSessions([
      ...stateRef.current.sessions.filter(item => item.id !== created.id),
      { ...created, message_count: 0, tool_count: 0, has_map: false },
    ]);
  }, [publishSessions]);

  const load = useCallback(async (createIfEmpty = false): Promise<void> => {
    const ctrl = controllerRef.current;
    if (!ctrl) return;
    const version = ++refreshVersion.current;
    const current = () => alive(ctrl) && version === refreshVersion.current;
    setLoading(true);
    try {
      let items = await apiListSessions(ctrl.signal);
      if (!current()) return;
      const selected = stateRef.current.activeId;
      let missing = false;
      if (selected && !items.some(item => item.id === selected)) {
        try {
          const retained = await apiGetSessionMeta(selected, ctrl.signal);
          items = [...items, retained];
        } catch (lookupError) {
          // A bounded recent listing is not deletion evidence. Only an
          // authoritative404 permits fallback; network failures retain focus.
          if (lookupError instanceof RuntimeApiError && lookupError.status === 404
              && lookupError.code === 'SESSION_NOT_FOUND') missing = true;
          else throw lookupError;
        }
      }
      if (!current()) return;
      const latest = stateRef.current.activeId;
      if (latest !== selected) {
        const retained = stateRef.current.sessions.find(item => item.id === latest);
        if (retained && !items.some(item => item.id === latest)) items = [...items, retained];
      } else if (missing || !latest) {
        selectSession(sortByUpdatedAt(items)[0]?.id ?? null);
      }
      publishSessions(items);
      if (createIfEmpty && !stateRef.current.activeId && items.length === 0) {
        const intent = selectionVersion.current;
        const created = await apiCreateSession(ctrl.signal);
        if (!current()) return;
        addCreated(created);
        if (intent === selectionVersion.current && !stateRef.current.activeId) selectSession(created.id);
      }
      setError(null);
    } catch (failure) {
      if (current() && !(failure instanceof Error && failure.name === 'AbortError')) {
        setError(failure instanceof Error ? failure.message : String(failure));
      }
    } finally {
      if (current()) setLoading(false);
    }
  }, [addCreated, alive, publishSessions, selectSession]);

  const refresh = useCallback(() => load(), [load]);

  const create = useCallback(async (): Promise<string> => {
    const ctrl = controllerRef.current;
    if (!ctrl) throw new Error('useSessions: unmounted');
    const intent = ++selectionVersion.current;
    ++refreshVersion.current;
    setLoading(false);
    try {
      const created = await apiCreateSession(ctrl.signal);
      if (!alive(ctrl)) throw new DOMException('Session view unmounted', 'AbortError');
      addCreated(created);
      if (intent === selectionVersion.current) selectSession(created.id);
      setError(null);
      await load();
      return created.id;
    } catch (failure) {
      if (!alive(ctrl) || failure instanceof Error && failure.name === 'AbortError') {
        throw new Error('useSessions: aborted');
      }
      setError(failure instanceof Error ? failure.message : String(failure));
      throw failure;
    }
  }, [addCreated, alive, load, selectSession]);

  const rename = useCallback(async (id: string, title: string): Promise<void> => {
    const ctrl = controllerRef.current;
    if (!ctrl) return;
    try {
      await apiRenameSession(id, title, ctrl.signal);
      if (!alive(ctrl)) return;
      publishSessions(stateRef.current.sessions.map(item => item.id === id ? { ...item, title } : item));
      setError(null);
      void load();
    } catch (failure) {
      if (!alive(ctrl) || failure instanceof Error && failure.name === 'AbortError') return;
      setError(failure instanceof Error ? failure.message : String(failure));
      throw failure;
    }
  }, [alive, load, publishSessions]);

  const remove = useCallback(async (id: string): Promise<void> => {
    const ctrl = controllerRef.current;
    if (!ctrl) return;
    // Reserve selection intent when the user acts. A completed DELETE must
    // not invalidate a newer create that is still awaiting its response.
    const intent = stateRef.current.activeId === id
      ? ++selectionVersion.current : selectionVersion.current;
    try {
      await apiDeleteSession(id, ctrl.signal);
      if (!alive(ctrl)) return;
    } catch (failure) {
      if (!alive(ctrl) || failure instanceof Error && failure.name === 'AbortError') return;
      setError(failure instanceof Error ? failure.message : String(failure));
      throw failure;
    }
    ++refreshVersion.current;
    setLoading(false);
    const remaining = sortByUpdatedAt(stateRef.current.sessions.filter(item => item.id !== id));
    publishSessions(remaining);
    clearMessageCache(id);
    setError(null);
    // Decide at completion, not from the selection before awaiting DELETE.
    if (stateRef.current.activeId !== id) return;
    selectSession(remaining[0]?.id ?? null);
    if (remaining.length || intent !== selectionVersion.current) return;
    try {
      const created = await apiCreateSession(ctrl.signal);
      if (!alive(ctrl)) return;
      addCreated(created);
      if (intent === selectionVersion.current && !stateRef.current.activeId) selectSession(created.id);
    } catch (failure) {
      if (!alive(ctrl) || failure instanceof Error && failure.name === 'AbortError') return;
      setError(failure instanceof Error ? failure.message : String(failure));
    }
  }, [addCreated, alive, publishSessions, selectSession]);

  const activate = useCallback((id: string): void => {
    const ctrl = controllerRef.current;
    if (!ctrl || !alive(ctrl)) return;
    ++selectionVersion.current;
    selectSession(id);
  }, [alive, selectSession]);

  useEffect(() => {
    const ctrl = new AbortController();
    controllerRef.current = ctrl;
    selectSession(getActiveSessionId());
    void load(true);
    return () => {
      ctrl.abort();
      if (controllerRef.current === ctrl) {
        controllerRef.current = null;
        ++refreshVersion.current;
      }
    };
  }, [load, selectSession]);

  return { sessions, activeId, loading, error, refresh, create, rename, remove, activate };
}
