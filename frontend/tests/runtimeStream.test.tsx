import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useRuntimeRun } from '@/hooks/useRuntimeRun';
import type { RunSnapshot, RuntimeEvent } from '@/types/runtime';

const encoder = new TextEncoder();
const snapshot = (last = 5): RunSnapshot => ({
  run_id: 'stream-run', session_id: 'stream-session', status: 'running', run_version: 1,
  tasks: [], output_artifacts: [], last_event_id: last + 100, last_seq: last,
});
const event = (seq: number, type = 'run.thought'): RuntimeEvent => ({
  run_id: 'stream-run', event_id: seq + 100, seq, type, payload: { message: '南京医院已完成' },
});
const frame = (value: RuntimeEvent, ending = '\n\n') => `id: ${value.event_id}\ndata: ${JSON.stringify(value)}${ending}`;

function response(bytes: Uint8Array, bytewise = false): Response {
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      if (bytewise) for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
      else controller.enqueue(bytes);
      controller.close();
    },
  }), { headers: { 'Content-Type': 'text/event-stream' } });
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function networkState(initial = true) {
  let online = initial;
  vi.stubGlobal('navigator', { get onLine() { return online; } });
  return async (value: boolean) => {
    await act(async () => { online = value; window.dispatchEvent(new Event(value ? 'online' : 'offline')); });
  };
}

it('aborts live browser transport offline and automatically restores the same run without writes', async () => {
  const setOnline = networkState();
  const cancel = vi.fn();
  const requests: Array<{ url: string; method: string; cursor: string | null; signal?: AbortSignal | null }> = [];
  let snapshots = 0;
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    requests.push({ url, method: init?.method ?? 'GET', cursor: new Headers(init?.headers).get('Last-Event-ID'), signal: init?.signal });
    if (!url.endsWith('/events')) return Response.json(snapshot(++snapshots === 1 ? 5 : 6));
    if (requests.filter(item => item.url.endsWith('/events')).length === 1) return new Response(new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(encoder.encode(frame(event(6)))); }, cancel,
    }));
    return response(encoder.encode(frame(event(7, 'run.completed'))));
  }));
  const { result } = renderHook(() => useRuntimeRun('stream-run'));
  await waitFor(() => expect(result.current.projection?.last_seq).toBe(6));
  await setOnline(false);
  expect(result.current.connectionState).toBe('reconnecting');
  expect(result.current.projection?.run_id).toBe('stream-run');
  expect(result.current.projection?.last_seq).toBe(6);
  expect(requests.find(item => item.url.endsWith('/events'))?.signal?.aborted).toBe(true);
  expect(cancel).toHaveBeenCalledOnce();
  const offlineCount = requests.length;
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 600)); });
  expect(requests).toHaveLength(offlineCount);
  await setOnline(true);
  await waitFor(() => expect(result.current.terminalSettled).toBe(true));
  expect(requests.filter(item => item.url.endsWith('/events')).map(item => item.cursor)).toEqual(['105', '106']);
  expect(requests.every(item => item.method === 'GET' && item.url.includes('/runs/stream-run'))).toBe(true);
  expect(result.current.projection?.last_seq).toBe(7);
});

it('does not request an initial snapshot while offline and does not resume after explicit stop', async () => {
  const setOnline = networkState(false);
  const fetch = vi.fn(async () => Response.json(snapshot()));
  vi.stubGlobal('fetch', fetch);
  const { result } = renderHook(() => useRuntimeRun('stream-run'));
  await waitFor(() => expect(result.current.connectionState).toBe('reconnecting'));
  expect(fetch).not.toHaveBeenCalled();
  await act(async () => result.current.stop());
  await setOnline(true);
  expect(fetch).not.toHaveBeenCalled();
  expect(result.current.connectionState).toBe('closed');
});

it('keeps terminal snapshots closed across offline and online events', async () => {
  const setOnline = networkState();
  const fetch = vi.fn(async () => Response.json({ ...snapshot(), status: 'completed' }));
  vi.stubGlobal('fetch', fetch);
  const { result } = renderHook(() => useRuntimeRun('stream-run'));
  await waitFor(() => expect(result.current.terminalSettled).toBe(true));
  await setOnline(false);
  await setOnline(true);
  expect(result.current.connectionState).toBe('closed');
  expect(result.current.terminalSettled).toBe(true);
  expect(fetch).toHaveBeenCalledOnce();
});

it('keeps a terminal gap-recovery snapshot closed across offline and online events', async () => {
  const setOnline = networkState();
  let snapshots = 0;
  const fetch = vi.fn(async (url: string) => {
    if (url.endsWith('/events')) return response(encoder.encode(frame(event(7))));
    snapshots += 1;
    return Response.json(snapshots === 1 ? snapshot(5) : { ...snapshot(7), status: 'completed' });
  });
  vi.stubGlobal('fetch', fetch);
  const { result } = renderHook(() => useRuntimeRun('stream-run'));
  await waitFor(() => expect(result.current.terminalSettled).toBe(true));
  expect(fetch).toHaveBeenCalledTimes(3);
  await setOnline(false);
  expect(result.current.connectionState).toBe('closed');
  expect(result.current.terminalSettled).toBe(true);
  await setOnline(true);
  expect(result.current.projection?.status).toBe('completed');
  expect(fetch).toHaveBeenCalledTimes(3);
});

it('ignores a late offline snapshot and removes recovery listeners when switching runs or unmounting', async () => {
  const setOnline = networkState();
  let finishOld!: (response: Response) => void;
  const requests: Array<{ url: string; signal?: AbortSignal | null }> = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    requests.push({ url, signal: init?.signal });
    if (url.includes('stream-run')) return new Promise<Response>(resolve => { finishOld = resolve; });
    return Response.json({ ...snapshot(20), run_id: 'new-run', status: 'completed' });
  }));
  const { result, rerender, unmount } = renderHook(({ runId }) => useRuntimeRun(runId), { initialProps: { runId: 'stream-run' } });
  await waitFor(() => expect(requests).toHaveLength(1));
  await setOnline(false);
  expect(requests[0].signal?.aborted).toBe(true);
  rerender({ runId: 'new-run' });
  await setOnline(true);
  await waitFor(() => expect(result.current.projection?.run_id).toBe('new-run'));
  await act(async () => finishOld(Response.json(snapshot(99))));
  expect(result.current.projection?.run_id).toBe('new-run');
  expect(result.current.projection?.last_seq).toBe(20);
  unmount();
  const count = requests.length;
  await setOnline(false);
  await setOnline(true);
  expect(requests).toHaveLength(count);
});

it('refreshes persisted task output details at a task checkpoint without refetching every SSE frame', async () => {
  let snapshots = 0;
  const task = { task_id: 't', task_key: '导出', status: 'running', attempts: 1 };
  const details = { parameters: { format: 'geojson' }, inputs: [], resolved_inputs: [], dependencies: [], outputs: [{ name: 'file', ordinal: 0, artifact_id: 'artifact' }] };
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (!url.endsWith('/events')) {
      snapshots += 1;
      return Response.json(snapshots === 1 ? { ...snapshot(), tasks: [task] } : { ...snapshot(7), tasks: [{ ...task, status: 'succeeded', details }] });
    }
    return response(encoder.encode(frame(event(6)) + frame({ ...event(7, 'task.completed'), task_id: 't', payload: {} }) + frame(event(8)) + frame(event(9, 'run.completed'))));
  }));
  const { result } = renderHook(() => useRuntimeRun('stream-run'));
  await waitFor(() => expect(result.current.terminalSettled).toBe(true));
  expect(result.current.projection?.tasks[0].details?.outputs[0].artifact_id).toBe('artifact');
  expect(snapshots).toBeLessThanOrEqual(3);
  expect(result.current.projection?.last_seq).toBe(9);
  expect(result.current.projection?.status).toBe('completed');
});

it('replays an unterminated frame after EOF without advancing the accepted cursor', async () => {
  const cursors: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
    if (!_url.endsWith('/events')) return Response.json(snapshot());
    cursors.push(new Headers(init?.headers).get('Last-Event-ID') ?? '');
    if (cursors.length === 1) return response(encoder.encode(frame(event(6), '')));
    return response(encoder.encode(frame(event(6)) + frame(event(7, 'run.completed'))));
  }));
  const { result } = renderHook(() => useRuntimeRun('stream-run'));
  await waitFor(() => expect(result.current.projection?.status).toBe('completed'), { timeout: 3000 });
  expect(cursors).toEqual(['105', '105']);
  expect(result.current.projection?.events.map(item => item.seq)).toEqual([6, 7]);
});

it('ignores snapshot-covered and repeated events while decoding split Chinese UTF-8 and CRLF', async () => {
  let snapshots = 0;
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (!url.endsWith('/events')) {
      snapshots += 1;
      return Response.json(snapshots === 1 ? snapshot() : { ...snapshot(7), status: 'completed' });
    }
    const stream = ': heartbeat\r\n\r\n' + [event(5), event(6), event(6), event(7, 'run.completed')]
      .map(value => frame(value).replaceAll('\n', '\r\n')).join('');
    return response(encoder.encode(stream), true);
  }));
  const { result } = renderHook(() => useRuntimeRun('stream-run'));
  await waitFor(() => expect(result.current.projection?.status).toBe('completed'));
  expect(snapshots).toBe(1);
  expect(result.current.projection?.events.map(item => item.seq)).toEqual([6, 7]);
  expect(result.current.projection?.events[0].payload.message).toBe('南京医院已完成');
  expect(result.current.error).toBeNull();
});

it('reconnects after a broken partial frame from the last complete event', async () => {
  const cursors: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (!url.endsWith('/events')) return Response.json(snapshot());
    cursors.push(new Headers(init?.headers).get('Last-Event-ID') ?? '');
    if (cursors.length === 1) return response(encoder.encode(frame(event(6)) + frame(event(7)).slice(0, 28)));
    return response(encoder.encode(frame(event(6)) + frame(event(7)) + frame(event(8, 'run.completed'))), true);
  }));
  const { result } = renderHook(() => useRuntimeRun('stream-run'));
  await waitFor(() => expect(result.current.projection?.status).toBe('completed'), { timeout: 3000 });
  expect(cursors).toEqual(['105', '106']);
  expect(result.current.projection?.events.map(item => item.seq)).toEqual([6, 7, 8]);
});

it('rebases a true event gap on the authoritative snapshot before tailing', async () => {
  let snapshots = 0;
  const cursors: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (!url.endsWith('/events')) return Response.json(snapshot(++snapshots === 1 ? 5 : 7));
    cursors.push(new Headers(init?.headers).get('Last-Event-ID') ?? '');
    return response(encoder.encode(frame(event(cursors.length === 1 ? 7 : 8, 'run.completed'))));
  }));
  const { result } = renderHook(() => useRuntimeRun('stream-run'));
  await waitFor(() => expect(result.current.projection?.status).toBe('completed'));
  expect(cursors).toEqual(['105', '107']);
  expect(result.current.projection?.events.map(item => item.seq)).toEqual([8]);
  expect(result.current.error).toBeNull();
  await act(async () => result.current.stop());
});

it('cancels an idle response body when the Run subscription is stopped', async () => {
  const cancel = vi.fn();
  let streams = 0;
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (!url.endsWith('/events')) return Response.json(snapshot());
    streams += 1;
    return new Response(new ReadableStream<Uint8Array>({ cancel }));
  }));
  const { result } = renderHook(() => useRuntimeRun('stream-run'));
  await waitFor(() => expect(streams).toBe(1));
  await act(async () => result.current.stop());
  expect(cancel).toHaveBeenCalledOnce();
});

it('reconnects between terminal status projection and the committed terminal event', async () => {
  const cursors: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (!url.endsWith('/events')) return Response.json(snapshot());
    cursors.push(new Headers(init?.headers).get('Last-Event-ID') ?? '');
    if (cursors.length === 1) {
      return response(encoder.encode(frame({ ...event(6, 'run.status_changed'), payload: { status: 'completed' } })));
    }
    return response(encoder.encode(frame(event(7, 'run.completed'))));
  }));
  const { result } = renderHook(() => useRuntimeRun('stream-run'));
  await waitFor(() => expect(result.current.projection?.last_seq).toBe(7), { timeout: 3000 });
  expect(cursors).toEqual(['105', '106']);
  expect(result.current.projection?.events.map(item => item.type)).toEqual(['run.status_changed', 'run.completed']);
});
