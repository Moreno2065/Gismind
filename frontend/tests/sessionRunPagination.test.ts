import { afterEach, expect, it, vi } from 'vitest';
import { getSessionRuns } from '@/api/client';

const run = (index: number) => ({
  run_id: 'run_' + index, session_id: 'history', status: 'completed',
  run_version: 1, created_at: '2026-11-02T00:00:00Z', updated_at: '2026-11-02T00:00:00Z',
});
afterEach(() => vi.unstubAllGlobals());

it('loads every history page so the oldest results survive a cold recovery', async () => {
  const first = Array.from({ length: 200 }, (_, index) => run(200 - index));
  const fetch = vi.fn(async (url: string) => Response.json(url.endsWith('?before=run_1')
    ? { items: [run(0)] } : { items: first, next_cursor: 'run_1' }));
  vi.stubGlobal('fetch', fetch);
  const items = await getSessionRuns('history');
  expect(items).toHaveLength(201);
  expect(items[200].run_id).toBe('run_0');
  expect(fetch.mock.calls.map(([url]) => url)).toEqual([
    '/api/sessions/history/runs', '/api/sessions/history/runs?before=run_1',
  ]);
});

it('reports a failed later page instead of returning an apparently complete partial history', async () => {
  let calls = 0;
  vi.stubGlobal('fetch', vi.fn(async () => ++calls === 1
    ? Response.json({ items: [run(1)], next_cursor: 'run_1' })
    : Response.json({ error: { code: 'UNAVAILABLE', message: 'history unavailable' } }, { status: 503 })));
  await expect(getSessionRuns('history')).rejects.toThrow('history unavailable');
});

it('stops if the history request is aborted between pages', async () => {
  const controller = new AbortController();
  const fetch = vi.fn(async () => {
    controller.abort();
    return Response.json({ items: [run(1)], next_cursor: 'run_1' });
  });
  vi.stubGlobal('fetch', fetch);
  await expect(getSessionRuns('history', controller.signal)).rejects.toMatchObject({ name: 'AbortError' });
  expect(fetch).toHaveBeenCalledTimes(1);
});

it('rejects repeated pagination cursors instead of looping forever', async () => {
  const fetch = vi.fn(async () => Response.json({ items: [run(1)], next_cursor: 'run_1' }));
  vi.stubGlobal('fetch', fetch);
  await expect(getSessionRuns('history')).rejects.toThrow('Invalid session run pagination');
  expect(fetch).toHaveBeenCalledTimes(2);
});
