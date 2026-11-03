import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ChatPanel } from '@/components/ChatPanel';
import type { RunSnapshot, RuntimeEvent } from '@/types/runtime';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(complete => { resolve = complete; });
  return { promise, resolve };
}

function json(value: unknown) { return Response.json(value); }

afterEach(() => { cleanup(); localStorage.clear(); vi.unstubAllGlobals(); });

it('keeps the recovered Run connected when the previous Run final snapshot arrives late', async () => {
  localStorage.clear();
  HTMLElement.prototype.scrollTo = vi.fn();
  const result = { artifact_id: 'art_a', type: 'File', media_type: 'application/octet-stream', validation_status: 'valid' };
  const runA: RunSnapshot = {
    run_id: 'run_a', session_id: 's', status: 'completed', run_version: 3,
    tasks: [], output_artifacts: [result], final_artifact_ids: ['art_a'], last_event_id: 10, last_seq: 10,
  };
  const runB: RunSnapshot = {
    run_id: 'run_b', session_id: 's', status: 'running', run_version: 2,
    tasks: [], output_artifacts: [], last_event_id: 20, last_seq: 10,
  };
  localStorage.setItem('gismind.active_run.s', JSON.stringify({ runId: 'run_a', messageId: 'assistant_run_a' }));
  const sessionRuns = deferred<Response>();
  const finalA = deferred<Response>();
  let aReads = 0;
  let bSignal: AbortSignal | null | undefined;
  let bEvents!: ReadableStreamDefaultController<Uint8Array>;
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const url = String(input);
    if (url === '/api/sessions/s/messages') return json({ messages: [] });
    if (url === '/api/sessions/s/runs') return sessionRuns.promise;
    if (url === '/api/runs/run_a') {
      aReads += 1;
      return aReads === 2 ? finalA.promise : json(runA);
    }
    if (url === '/api/runs/run_b') return json(runB);
    if (url === '/api/runs/run_b/events') {
      bSignal = init?.signal;
      return new Response(new ReadableStream<Uint8Array>({
        start(controller) {
          bEvents = controller;
          init?.signal?.addEventListener('abort', () => { try { controller.close(); } catch { /* already closed */ } });
        },
      }));
    }
    if (url === '/api/artifacts/art_a') return json(result);
    throw new Error(`Unexpected request: ${init?.method ?? 'GET'} ${url}`);
  }));

  render(<ChatPanel sessionId="s" />);
  await waitFor(() => expect(aReads).toBe(2));
  await act(async () => sessionRuns.resolve(json({ items: [runB, runA] })));
  await waitFor(() => expect(bEvents).toBeDefined());
  expect(JSON.parse(localStorage.getItem('gismind.active_run.s') ?? 'null')).toEqual({ runId: 'run_b', messageId: 'assistant_run_b' });
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(true);

  await act(async () => finalA.resolve(json(runA)));

  expect(JSON.parse(localStorage.getItem('gismind.active_run.s') ?? 'null')).toEqual({ runId: 'run_b', messageId: 'assistant_run_b' });
  expect(bSignal?.aborted).toBe(false);
  expect(screen.getByTestId('runtime-progress').getAttribute('data-run-id')).toBe('run_b');
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(true);
  expect(screen.getByRole('button', { name: '停止' })).toBeTruthy();
  expect(screen.getByRole('button', { name: '选择成果 art_a' })).toBeTruthy();

  const event: RuntimeEvent = {
    run_id: 'run_b', event_id: 21, seq: 11, type: 'task.created',
    payload: { task_id: 'task_b', task_key: 'B 仍在执行', initial_status: 'running' },
  };
  await act(async () => bEvents.enqueue(new TextEncoder().encode(`id: 21\ndata: ${JSON.stringify(event)}\n\n`)));
  await screen.findByText('B 仍在执行');
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(true);
});
