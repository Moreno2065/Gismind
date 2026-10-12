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
