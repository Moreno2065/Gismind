import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useRuntimeRun } from '@/hooks/useRuntimeRun';
import type { RunSnapshot, RuntimeEvent } from '@/types/runtime';

const encoder = new TextEncoder();
const snapshot = (runId = 'run-a', status: RunSnapshot['status'] = 'running'): RunSnapshot => ({
  run_id: runId, session_id: 'session', status, run_version: 1,
  tasks: [], output_artifacts: [], last_event_id: 100, last_seq: 1,
});
const frame = (value: RuntimeEvent) => encoder.encode(`data: ${JSON.stringify(value)}\n\n`);
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('settles only after the accepted named terminal frame and keeps recovery separate from run status', async () => {
  const controllers: ReadableStreamDefaultController<Uint8Array>[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (!url.endsWith('/events')) return Response.json(snapshot());
    return new Response(new ReadableStream<Uint8Array>({ start(controller) { controllers.push(controller); } }));
  }));
  const { result } = renderHook(() => useRuntimeRun('run-a'));
  await waitFor(() => expect(controllers).toHaveLength(1));
  expect(result.current.terminalSettled).toBe(false);
  expect(result.current.connectionState).toBe('live');
  await act(async () => {
    controllers[0].enqueue(frame({ run_id: 'run-a', event_id: 101, seq: 2, type: 'run.status_changed', payload: { status: 'completed' } }));
    controllers[0].close();
  });
  expect(result.current.projection?.status).toBe('completed');
  expect(result.current.terminalSettled).toBe(false);
  expect(result.current.connectionState).toBe('reconnecting');
  await waitFor(() => expect(controllers).toHaveLength(2));
  await act(async () => {
    controllers[1].enqueue(frame({ run_id: 'other-run', event_id: 102, seq: 3, type: 'run.completed', payload: {} }));
  });
  expect(result.current.terminalSettled).toBe(false);
  await act(async () => {
    const terminal = frame({ run_id: 'run-a', event_id: 102, seq: 3, type: 'run.completed', payload: {} });
    controllers[1].enqueue(terminal.slice(0, terminal.length - 1));
  });
  expect(result.current.terminalSettled).toBe(false);
  await act(async () => { controllers[1].enqueue(encoder.encode('\n')); });
  expect(result.current.terminalSettled).toBe(true);
  expect(result.current.connectionState).toBe('closed');
});

it('does not reuse settlement or connection state across run changes', async () => {
  let resolveNext!: (value: Response) => void;
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.endsWith('/run-a')) return Response.json(snapshot('run-a', 'completed'));
    if (url.endsWith('/run-b')) return new Promise<Response>(resolve => { resolveNext = resolve; });
    return new Response(new ReadableStream<Uint8Array>());
  }));
  const { result, rerender } = renderHook(({ id }) => useRuntimeRun(id), { initialProps: { id: 'run-a' as string | null } });
  await waitFor(() => expect(result.current.terminalSettled).toBe(true));
  expect(result.current.connectionState).toBe('closed');
  rerender({ id: 'run-b' });
  expect(result.current.terminalSettled).toBe(false);
  expect(result.current.connectionState).toBe('connecting');
  expect(result.current.projection).toBeNull();
  await act(async () => resolveNext(Response.json(snapshot('run-b'))));
  await waitFor(() => expect(result.current.connectionState).toBe('live'));
  expect(result.current.terminalSettled).toBe(false);
  rerender({ id: null });
  expect(result.current.terminalSettled).toBe(false);
  expect(result.current.connectionState).toBe('idle');
});
