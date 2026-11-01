import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { useSessions } from '@/hooks/useSessions';
import { createSession, getSessionMeta, listSessions } from '@/api/client';

const ACTIVE = 'gismind.active_session';
const meta = (id: string, time = 1) => ({
  id, title: id, created_at: time, updated_at: time,
  message_count: 0, tool_count: 0, has_map: false,
});
const json = (value: unknown, status = 200) => Response.json(value, { status });
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
beforeEach(() => { localStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('restores an existing active session outside the recent200 listing without creating or switching sessions', async () => {
  localStorage.setItem(ACTIVE, 'older');
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method) throw new Error('Unexpected write');
    if (url === '/api/sessions') return json({ items: Array.from({ length: 200 }, (_, i) => meta('recent' + i, 300 - i)) });
    if (url === '/api/sessions/older') return json(meta('older'));
    throw new Error('Unexpected URL: ' + url);
  });
  vi.stubGlobal('fetch', fetch);
  const { result } = renderHook(() => useSessions());
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.activeId).toBe('older');
  expect(result.current.sessions.some(s => s.id === 'older')).toBe(true);
  expect(localStorage.getItem(ACTIVE)).toBe('older');
  expect(result.current.error).toBeNull();
});

it('shows a failed initial automatic create instead of clearing its error', async () => {
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => init?.method === 'POST'
    ? json({ error: { code: 'CREATE_FAILED', message: 'creation unavailable' } }, 503)
    : json({ items: [] })));
  const { result } = renderHook(() => useSessions());
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.error).toContain('creation unavailable');
  expect(result.current.activeId).toBeNull();
});

it('does not let a pending deletion replace a later explicit session choice', async () => {
  localStorage.setItem(ACTIVE, 'a');
  const pending = deferred<Response>();
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => init?.method === 'DELETE'
    ? pending.promise : json({ items: [meta('a', 3), meta('b', 2), meta('c', 1)] })));
  const { result } = renderHook(() => useSessions());
  await waitFor(() => expect(result.current.activeId).toBe('a'));
  let removing!: Promise<void>;
  act(() => { removing = result.current.remove('a'); });
  act(() => result.current.activate('c'));
  await act(async () => { pending.resolve(new Response(null, { status: 204 })); await removing; });
  expect(result.current.activeId).toBe('c');
  expect(localStorage.getItem(ACTIVE)).toBe('c');
  expect(result.current.sessions.map(s => s.id)).toEqual(['b', 'c']);
});

it('does not let an older list response overwrite a newer refresh result', async () => {
  localStorage.setItem(ACTIVE, 'a');
  const old = deferred<Response>(), latest = deferred<Response>();
  let calls = 0;
  vi.stubGlobal('fetch', vi.fn(async () => {
    calls += 1;
    if (calls === 1) return json({ items: [meta('a')] });
    return calls === 2 ? old.promise : latest.promise;
  }));
  const { result } = renderHook(() => useSessions());
  await waitFor(() => expect(result.current.activeId).toBe('a'));
  let first!: Promise<void>, second!: Promise<void>;
  act(() => { first = result.current.refresh(); second = result.current.refresh(); });
  await act(async () => { latest.resolve(json({ items: [{ ...meta('a'), title: 'latest' }] })); await second; });
  await act(async () => { old.resolve(json({ items: [{ ...meta('a'), title: 'stale' }] })); await first; });
  expect(result.current.sessions[0].title).toBe('latest');
  expect(result.current.loading).toBe(false);
});

it('does not let a pending create steal a later explicit session selection', async () => {
  localStorage.setItem(ACTIVE, 'a');
  const pending = deferred<Response>();
  let created = false;
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === 'POST') return pending.promise;
    return json({ items: [...(created ? [meta('new', 4)] : []), meta('a', 3), meta('c', 2)] });
  }));
  const { result } = renderHook(() => useSessions());
  await waitFor(() => expect(result.current.activeId).toBe('a'));
  let creating!: Promise<string>;
  act(() => { creating = result.current.create(); });
  act(() => result.current.activate('c'));
  await act(async () => { created = true; pending.resolve(json(meta('new', 4), 201)); await creating; });
  expect(result.current.activeId).toBe('c');
  expect(localStorage.getItem(ACTIVE)).toBe('c');
  expect(result.current.sessions.some(s => s.id === 'new')).toBe(true);
});

it.each([false, true])('preserves a later create intent when DELETE finishes first (last session: %s)', async (lastSession) => {
  localStorage.setItem(ACTIVE, 'a');
  const deletion = deferred<Response>(), creation = deferred<Response>();
  let deleted = false, created = false;
  const fetch = vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === 'DELETE') return deletion.promise;
    if (init?.method === 'POST') return creation.promise;
    return json({ items: [
      ...(created ? [meta('new', 4)] : []),
      ...(deleted ? [] : [meta('a', 3)]),
      ...(lastSession ? [] : [meta('b', 2)]),
    ] });
  });
  vi.stubGlobal('fetch', fetch);
  const { result } = renderHook(() => useSessions());
  await waitFor(() => expect(result.current.loading).toBe(false));
  let removing!: Promise<void>, creating!: Promise<string>;
  act(() => { removing = result.current.remove('a'); });
  act(() => { creating = result.current.create(); });
  await act(async () => {
    deleted = true;
    deletion.resolve(new Response(null, { status: 204 }));
    // Flush the DELETE response without awaiting an unwanted replacement POST.
    await Promise.resolve();
  });
  expect(fetch.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
  await act(async () => {
    created = true;
    creation.resolve(json(meta('new', 4), 201));
    await Promise.all([removing, creating]);
  });
  expect(result.current.activeId).toBe('new');
  expect(localStorage.getItem(ACTIVE)).toBe('new');
  expect(result.current.sessions.some(item => item.id === 'a')).toBe(false);
});

it('preserves the stored session on a transient metadata lookup failure instead of silently falling back', async () => {
  localStorage.setItem(ACTIVE, 'older');
  vi.stubGlobal('fetch', vi.fn(async (url: string) => url === '/api/sessions'
    ? json({ items: [meta('recent', 3)] })
    : json({ error: { code: 'TEMPORARY_FAILURE', message: 'try again' } }, 503)));
  const { result } = renderHook(() => useSessions());
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.activeId).toBe('older');
  expect(localStorage.getItem(ACTIVE)).toBe('older');
  expect(result.current.error).toContain('try again');
});

it('falls back only after the selected session is confirmed missing', async () => {
  localStorage.setItem(ACTIVE, 'missing');
  vi.stubGlobal('fetch', vi.fn(async (url: string) => url === '/api/sessions'
    ? json({ items: [meta('recent', 3)] })
    : json({ error: { code: 'SESSION_NOT_FOUND', message: 'missing' } }, 404)));
  const { result } = renderHook(() => useSessions());
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.activeId).toBe('recent');
  expect(result.current.error).toBeNull();
});

it('clears a deleted last session even when creating its replacement fails', async () => {
  localStorage.setItem(ACTIVE, 'a');
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === 'DELETE') return new Response(null, { status: 204 });
    if (init?.method === 'POST') return json({ error: { code: 'CREATE_FAILED', message: 'unavailable' } }, 503);
    return json({ items: [meta('a')] });
  }));
  const { result } = renderHook(() => useSessions());
  await waitFor(() => expect(result.current.activeId).toBe('a'));
  await act(async () => { await result.current.remove('a'); });
  expect(result.current.activeId).toBeNull();
  expect(localStorage.getItem(ACTIVE)).toBeNull();
  expect(result.current.error).toContain('unavailable');
});

it.each(['list', 'single', 'create'] as const)('normalizes actual Runtime ISO timestamps for the %s session API', async (operation) => {
  const created = '2026-10-31T12:00:00.000Z', updated = '2026-11-01T04:30:00+00:00';
  const item = { ...meta('a'), created_at: created, updated_at: updated };
  vi.stubGlobal('fetch', vi.fn(async () => json(operation === 'list' ? { items: [item] } : item)));
  const result = operation === 'list' ? (await listSessions())[0]
    : operation === 'single' ? await getSessionMeta('a') : await createSession();
  expect(result.created_at).toBe(Date.parse(created));
  expect(result.updated_at).toBe(Date.parse(updated));
});
