import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ChatPanel } from '@/components/ChatPanel';
import { MessageBubble } from '@/components/MessageBubble';
import type { RunSnapshot } from '@/types/runtime';

vi.mock('@/components/LazyMapView', () => ({ LazyMapView: () => <div aria-label="恢复的地图" /> }));
const crs = { kind: 'EPSG', code: 4326 };
const artifact = { artifact_id: 'art_saved', type: 'MapLayer', crs, validation_status: 'valid', byte_size: 240 };
const map = { crs, bbox: [116, 39, 117, 40], layers: [{ type: 'FeatureCollection', features: [{ type: 'Feature', geometry: { type: 'Point', coordinates: [116.4, 39.9] }, properties: {} }] }] };
const run: RunSnapshot = { run_id: 'r_saved', session_id: 's', status: 'completed', run_version: 4, tasks: [], output_artifacts: [artifact], last_event_id: 12, last_seq: 12 };
const requests: string[] = [];
let failContent = false;
let eventController: ReadableStreamDefaultController<Uint8Array> | undefined;
let running = false;
let finalBody: Record<string, unknown> | undefined;
function json(value: unknown) { return new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } }); }
beforeEach(() => {
  localStorage.clear(); requests.length = 0; failContent = false; running = false; eventController = undefined; finalBody = undefined;
  HTMLElement.prototype.scrollTo = vi.fn();
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    requests.push(`${init?.method ?? 'GET'} ${url}`);
    if (url.endsWith('/messages')) return json({ messages: [{ id: 'server_user', role: 'user', content: '服务器上的新消息' }] });
    if (url.endsWith('/runs') && (init?.method ?? 'GET') === 'GET') return json({ items: [running ? { ...run, status: 'running' } : run] });
    if (url === '/api/runs' && init?.method === 'POST') { finalBody = JSON.parse(String(init.body)); return json({ ...run, run_id: 'r_new' }); }
    if (url.endsWith('/events')) return new Response(new ReadableStream<Uint8Array>({ start(controller) { eventController = controller; init?.signal?.addEventListener('abort', () => { try { controller.close(); } catch { /* closed */ } }); } }));
    if (url.includes('/runs/')) return json(running ? { ...run, status: 'running', output_artifacts: [], last_seq: 10, last_event_id: 10 } : run);
    if (url.endsWith('/content')) return failContent ? new Response('{}', { status: 503 }) : json(map);
    if (url.endsWith('/artifacts/art_saved')) return json(artifact);
    throw new Error(`Unexpected ${url}`);
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('Durable workspace recovery', () => {
  it('reloads a cached map from its Artifact instead of requesting a new Run', async () => {
    render(<MessageBubble message={{ id: 'm', role: 'assistant', status: 'done', created_at: 1, blocks: [{ type: 'map', artifact_id: 'art_saved', expired: true, layers: [], bbox: [0, 0, 1, 1] }] }} />);
    fireEvent.click(screen.getByRole('button', { name: '加载历史地图' }));
    await screen.findByLabelText('恢复的地图');
    expect(requests).toEqual(['GET /api/artifacts/art_saved', 'GET /api/artifacts/art_saved/content']);
  });
  it('keeps the artifact reference retryable when content retrieval fails', async () => {
    failContent = true;
    render(<MessageBubble message={{ id: 'm', role: 'assistant', status: 'done', created_at: 1, blocks: [{ type: 'map', artifact_id: 'art_saved', expired: true, layers: [], bbox: [0, 0, 1, 1] }] }} />);
    fireEvent.click(screen.getByRole('button', { name: '加载历史地图' }));
    await screen.findByRole('alert'); failContent = false;
    fireEvent.click(screen.getByRole('button', { name: '重试加载历史地图' }));
    await screen.findByLabelText('恢复的地图');
    expect(requests.every(item => item.startsWith('GET '))).toBe(true);
  });
  it('restores completed-run artifacts without local storage and reuses them only after selection', async () => {
    render(<ChatPanel sessionId="s" />);
    await waitFor(() => expect(requests).toContain('GET /api/runs/r_saved'));
    fireEvent.click(await screen.findByRole('button', { name: /用于下一步/ }));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '继续分析选择的成果' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(finalBody?.input_artifact_ids).toEqual(['art_saved']));
  });
  it('does not let a nonempty presentation cache hide server messages', async () => {
    localStorage.setItem('gismind.messages.s', JSON.stringify([{ id: 'cached', role: 'user', status: 'done', created_at: 1, blocks: [{ type: 'text', content: '本地缓存消息' }] }]));
    render(<ChatPanel sessionId="s" />);
    await screen.findByText('服务器上的新消息');
  });
  it('waits through generic terminal state for the durable terminal event and its artifacts', async () => {
    running = true;
    render(<ChatPanel sessionId="s" />);
    await waitFor(() => expect(eventController).toBeDefined());
    function emit(seq: number, event_type: string, payload: unknown) {
      eventController!.enqueue(new TextEncoder().encode(`id: ${seq}\nevent: ${event_type}\ndata: ${JSON.stringify({ event_id: seq, seq, run_id: 'r_saved', type: event_type, payload, schema_version: '1' })}\n\n`));
    }
    await act(async () => emit(11, 'run.status_changed', { status: 'completed', run_version: 4 }));
    expect(localStorage.getItem('gismind.active_run.s')).not.toBeNull();
    running = false;
    await act(async () => emit(12, 'run.completed', { status: 'completed', output_artifacts: [artifact] }));
    await waitFor(() => expect(localStorage.getItem('gismind.active_run.s')).toBeNull());
    expect(await screen.findByRole('button', { name: /用于下一步/ })).toBeTruthy();
  });
  it('retrying history recovery does not abort an in-flight upload or strand its state', async () => {
    const originalFetch = globalThis.fetch;
    let uploadSignal: AbortSignal | null | undefined;
    let finish!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/messages')) return new Response('{}', { status: 503 });
      if (url.endsWith('/uploads')) {
        uploadSignal = init?.signal;
        return new Promise<Response>(resolve => { finish = resolve; });
      }
      return originalFetch(url, init);
    }));
    const view = render(<ChatPanel sessionId="s" />);
    await screen.findByRole('button', { name: '重试恢复' });
    fireEvent.change(view.container.querySelector('input[type=file]')!, { target: { files: [new File(['{}'], 'pending.json')] } });
    await waitFor(() => expect(finish).toBeDefined());
    fireEvent.click(screen.getByRole('button', { name: '重试恢复' }));
    expect(uploadSignal?.aborted).toBe(false);
    await act(async () => finish(json({ artifact_id: 'uploaded', type: 'File' })));
    await screen.findByText('pending.json · 就绪');
  });
  it('hydrates the CRS of a MapLayer announced by a real-shaped artifact.published event', async () => {
    running = true;
    render(<ChatPanel sessionId="s" />);
    await waitFor(() => expect(eventController).toBeDefined());
    await act(async () => eventController!.enqueue(new TextEncoder().encode(`id: 11\nevent: artifact.published\ndata: ${JSON.stringify({ event_id: 11, seq: 11, run_id: 'r_saved', type: 'artifact.published', artifact_id: 'art_saved', task_id: 'map', schema_version: '1', payload: { artifact_type: 'MapLayer' } })}\n\n`)));
    await waitFor(() => expect(requests.filter(item => item === 'GET /api/artifacts/art_saved')).toHaveLength(2));
    await waitFor(() => expect(screen.getAllByLabelText('恢复的地图')).toHaveLength(2));
    expect(screen.queryByText(/地图加载失败/)).toBeNull();
  });
});
