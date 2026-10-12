import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ChatPanel } from '@/components/ChatPanel';
import { projectionFromSnapshot, reduceRuntimeEvent, useRuntimeRun } from '@/hooks/useRuntimeRun';
import type { RunSnapshot } from '@/types/runtime';

vi.mock('@/components/LazyMapView', () => ({ LazyMapView: ({ layers }: { layers: unknown }) => <div aria-label="地图">{JSON.stringify(layers)}</div> }));
const snapshots = new Map<string, RunSnapshot>();
const requests: Array<{ url: string; body: Record<string, unknown> }> = [];
let nextSnapshots: RunSnapshot[];
let serverRuns: RunSnapshot[];
let mapContent: Promise<unknown>;
let streamCursorFailure: boolean;
let rejectCancel: boolean;
let keepAwaiting: boolean;
let uploadResponse: () => Promise<Response>;
let rejectCreate: boolean;
let loseCreateResponse: boolean;
let createResponseBarrier: Promise<void> | null;
const createdByRequestId = new Map<string, RunSnapshot>();
function snapshot(id: string, status: RunSnapshot['status'] = 'running', extra: Partial<RunSnapshot> = {}): RunSnapshot {
  return { run_id: id, session_id: 's', status, run_version: 3, tasks: [], output_artifacts: [], last_event_id: 10, last_seq: 10, awaiting_input: null, pending_confirmations: [], diagnostic: null, patch_schema: null, ...extra };
}
function paused(id: string, schema: Record<string, unknown> = { type: 'object' }) {
  return snapshot(id, 'awaiting_input', { awaiting_input: { message: '请补充坐标系', patch_schema: schema }, patch_schema: schema });
}
function restore(run: RunSnapshot) {
  snapshots.set(run.run_id, run);
  localStorage.setItem('gismind.active_run.s', JSON.stringify({ runId: run.run_id, messageId: `assistant_${run.run_id}` }));
}
function json(value: unknown) { return new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } }); }
beforeEach(() => {
  localStorage.clear(); snapshots.clear(); requests.length = 0; nextSnapshots = []; serverRuns = [];
  streamCursorFailure = false; rejectCancel = false; keepAwaiting = false;
  rejectCreate = false; uploadResponse = async () => json({ artifact_id: 'uploaded-file', type: 'File' });
  loseCreateResponse = false; createResponseBarrier = null; createdByRequestId.clear();
  mapContent = Promise.resolve({ layers: [], bbox: [0, 0, 1, 1] });
  HTMLElement.prototype.scrollTo = vi.fn();
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const url = String(input); const method = init?.method ?? 'GET';
    const body = init?.body && typeof init.body === 'string' ? JSON.parse(init.body) : {};
    requests.push({ url: `${method} ${url}`, body });
    if (url.endsWith('/messages')) return json({ messages: [] });
    if (/\/sessions\/[^/]+\/runs$/.test(url)) return json({ items: serverRuns });
    if (url === '/api/artifacts/uploads' && method === 'POST') return uploadResponse();
    if (url === '/api/runs' && method === 'POST') {
      if (rejectCreate) return new Response(JSON.stringify({ error: { code: 'UNAVAILABLE', message: '暂时不可用' } }), { status: 503 });
      const existing = createdByRequestId.get(body.client_request_id);
      if (existing) return json(existing);
      const run = nextSnapshots.shift() ?? snapshot(`run_${snapshots.size + 1}`, 'running', { session_id: body.session_id });
      snapshots.set(run.run_id, run); createdByRequestId.set(body.client_request_id, run);
      if (createResponseBarrier) await createResponseBarrier;
      if (loseCreateResponse) { loseCreateResponse = false; throw new TypeError('服务端创建后响应丢失'); }
      return json(run);
    }
    if (url.endsWith('/events') && streamCursorFailure) {
      streamCursorFailure = false;
      return new Response(JSON.stringify({ error: { code: 'INVALID_EVENT_CURSOR', message: 'cursor was removed' } }), { status: 409 });
    }
    if (url.endsWith('/events')) return new Response(new ReadableStream({ start(controller) {
      init?.signal?.addEventListener('abort', () => { try { controller.close(); } catch { /* closed */ } });
    } }));
    if (url.endsWith('/content')) return json(await mapContent);
    const artifactMatch = url.match(/\/artifacts\/([^/]+)$/);
    if (artifactMatch) {
      const artifact = [...snapshots.values()].flatMap(run => run.output_artifacts).find(item => item.artifact_id === artifactMatch[1]);
      return artifact ? json(artifact) : json({ artifact_id: artifactMatch[1], type: 'File' });
    }
    const match = url.match(/\/runs\/([^/]+)(?:\/(resume|confirm|cancel))?$/);
    if (match) {
      const run = snapshots.get(match[1]);
      if (!run) return new Response(JSON.stringify({ error: { code: 'RUN_NOT_FOUND', message: 'missing' } }), { status: 404 });
      if (match[2]) {
        if (match[2] === 'cancel' && rejectCancel) return new Response(JSON.stringify({ error: { code: 'RUN_VERSION_CONFLICT', message: 'refresh before retrying' } }), { status: 409 });
        if (match[2] === 'resume' && keepAwaiting) return json({ ...run, requires_user_decision: true, pending_task_ids: ['side-effect'] });
        const changed = { ...run, status: match[2] === 'cancel' ? 'cancelled' : 'running', run_version: run.run_version + 1, awaiting_input: null, patch_schema: null, pending_confirmations: [] } as RunSnapshot;
        snapshots.set(run.run_id, changed); return json(changed);
      }
      return json(run);
    }
    throw new Error(`Unexpected request: ${method} ${url}`);
  }));
});

describe('Create Run idempotency', () => {
  const creates = () => requests.filter(item => item.url === 'POST /api/runs');
  async function composer(withUpload = false) {
    const view = render(<ChatPanel sessionId="s" />);
    await screen.findByRole('button', { name: '发送' });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '分析同一任务' } });
    if (withUpload) {
      fireEvent.change(view.container.querySelector('input[type=file]')!, { target: { files: [new File(['x,y\n1,2'], 'input.csv')] } });
      await screen.findByText(/input.csv · 就绪/);
    }
    return view;
  }

  it('reuses the create ID after a committed response is lost and keeps the original attachments', async () => {
    loseCreateResponse = true;
    await composer(true);
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await screen.findByText(/服务端创建后响应丢失/);
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('分析同一任务');
    expect(screen.getByText(/input.csv · 就绪/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await screen.findByText('Run running');
    expect(creates()).toHaveLength(2);
    expect(creates()[1].body).toEqual(creates()[0].body);
    expect(createdByRequestId.size).toBe(1);
    expect(snapshots.size).toBe(1);
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('');
    expect(screen.queryByText(/input.csv · 就绪/)).toBeNull();
  });

  it('does not clear text or attachments while the create response is unresolved', async () => {
    let finish!: () => void;
    createResponseBarrier = new Promise(resolve => { finish = resolve; });
    await composer(true);
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(creates()).toHaveLength(1));
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('分析同一任务');
    expect(screen.getByText(/input.csv · 就绪/)).toBeTruthy();
    await act(async () => { finish(); });
    await screen.findByText('Run running');
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('');
    expect(screen.queryByText(/input.csv · 就绪/)).toBeNull();
  });

  it.each(['message', 'attachments', 'session'])('allocates a new create ID when the %s changes', async (changed) => {
    loseCreateResponse = true;
    const view = await composer(changed === 'attachments');
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await screen.findByText(/服务端创建后响应丢失/);
    if (changed === 'session') {
      view.rerender(<ChatPanel sessionId="other" />);
      await screen.findByRole('button', { name: '发送' });
      fireEvent.change(screen.getByRole('textbox'), { target: { value: '分析同一任务' } });
    } else if (changed === 'message') {
      fireEvent.change(screen.getByRole('textbox'), { target: { value: '这是修改后的任务' } });
    } else {
      fireEvent.click(screen.getByRole('button', { name: '移除 input.csv' }));
    }
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(creates()).toHaveLength(2));
    expect(creates()[1].body.client_request_id).not.toBe(creates()[0].body.client_request_id);
    expect(createdByRequestId.size).toBe(2);
  });

  it('retires a confirmed create ID before the next intentional identical task', async () => {
    nextSnapshots = [snapshot('first-confirmed', 'completed'), snapshot('next-confirmed', 'completed')];
    await composer();
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(requests.some(item => item.url === 'GET /api/runs/first-confirmed')).toBe(true));
    await waitFor(() => expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(false));
    await screen.findByRole('button', { name: '发送' });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '分析同一任务' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(creates()).toHaveLength(2));
    expect(creates()[1].body.client_request_id).not.toBe(creates()[0].body.client_request_id);
    expect(createdByRequestId.size).toBe(2);
  });
});

describe('Upload interactions', () => {
  async function composer() {
    const view = render(<ChatPanel sessionId="s" />);
    await screen.findByRole('button', { name: '发送' });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '分析上传文件' } });
    return view.container.querySelector('input[type=file]') as HTMLInputElement;
  }
  function selectFile(input: HTMLInputElement, name = 'points.csv') {
    fireEvent.change(input, { target: { files: [new File(['x,y\n1,2'], name, { type: 'text/csv' })] } });
  }
  it('blocks click and Enter until every attachment finishes uploading', async () => {
    let finish!: (value: Response) => void;
    uploadResponse = () => new Promise(resolve => { finish = resolve; });
    const picker = await composer(); selectFile(picker);
    expect((screen.getByRole('button', { name: '发送' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' });
    expect(requests.some(item => item.url === 'POST /api/runs')).toBe(false);
    await act(async () => { finish(json({ artifact_id: 'uploaded-file', type: 'File' })); });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(requests.find(item => item.url === 'POST /api/runs')?.body.input_artifact_ids).toEqual(['uploaded-file']));
  });
  it('shows an upload failure and allows retry without silently discarding the file', async () => {
    uploadResponse = async () => new Response(JSON.stringify({ error: { code: 'UPLOAD_FAILED', message: '连接中断' } }), { status: 503 });
    const picker = await composer(); selectFile(picker);
    const retry = await screen.findByRole('button', { name: '重试上传 points.csv' });
    expect(screen.getByText(/连接中断/)).toBeTruthy();
    expect((screen.getByRole('button', { name: '发送' }) as HTMLButtonElement).disabled).toBe(true);
    uploadResponse = async () => json({ artifact_id: 'retried-file', type: 'File' }); fireEvent.click(retry);
    await screen.findByText(/points.csv · 就绪/); fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(requests.find(item => item.url === 'POST /api/runs')?.body.input_artifact_ids).toEqual(['retried-file']));
  });
  it('requires removal of a failed attachment before sending without it', async () => {
    uploadResponse = async () => new Response(JSON.stringify({ error: { code: 'UPLOAD_FAILED', message: '文件读取失败' } }), { status: 422 });
    const picker = await composer(); selectFile(picker);
    await screen.findByText(/points.csv · 失败/);
    expect((screen.getByRole('button', { name: '发送' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '移除 points.csv' }));
    expect((screen.getByRole('button', { name: '发送' }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(requests.find(item => item.url === 'POST /api/runs')?.body.input_artifact_ids).toEqual([]));
  });
  it('keeps uploaded attachments when creating a Run fails', async () => {
    rejectCreate = true;
    const picker = await composer(); selectFile(picker);
    await screen.findByText(/points.csv · 就绪/); fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await screen.findByText('暂时不可用', { exact: false });
    expect(screen.getByText(/points.csv · 就绪/)).toBeTruthy();
    rejectCreate = false;
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '再次分析上传文件' } }); fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(requests.filter(item => item.url === 'POST /api/runs').at(-1)?.body.input_artifact_ids).toEqual(['uploaded-file']));
    await waitFor(() => expect(screen.queryByText(/points.csv · 就绪/)).toBeNull());
    expect(requests.filter(item => item.url === 'POST /api/artifacts/uploads')).toHaveLength(1);
  });
  it('allows selection of CSV, TSV, GPKG and files detected by content without an extension', async () => {
    const picker = await composer();
    expect(picker.accept).toBe('');
    selectFile(picker, 'points');
    await screen.findByText(/points · 就绪/);
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
describe('Runtime projection', () => {
  it('hides a detached terminal Run before the next Run snapshot arrives', async () => {
    snapshots.set('a', snapshot('a', 'completed')); snapshots.set('b', snapshot('b'));
    const { result, rerender } = renderHook(({ id }) => useRuntimeRun(id), { initialProps: { id: 'a' as string | null } });
    await waitFor(() => expect(result.current.projection?.run_id).toBe('a'));
    rerender({ id: null }); expect(result.current.projection).toBeNull();
    rerender({ id: 'b' }); expect(result.current.projection).toBeNull();
    await waitFor(() => expect(result.current.projection?.run_id).toBe('b'));
  });
  it('projects pending confirmations and removes the exact confirmed action', () => {
    let state = projectionFromSnapshot(snapshot('a', 'ready'));
    const confirmation = { confirmation_id: 'c', task_id: 't', task_key: 'publish', action_hash: 'a'.repeat(64) };
    state = reduceRuntimeEvent(state, { run_id: 'a', seq: 11, event_id: 11, type: 'run.confirmation_required', payload: confirmation });
    expect(state.pending_confirmations).toEqual([confirmation]);
    state = reduceRuntimeEvent(state, { run_id: 'a', seq: 12, event_id: 12, type: 'run.confirmed', payload: confirmation });
    expect(state.pending_confirmations).toEqual([]);
  });
  it('keeps schema and diagnostic consistent with the current pause', () => {
    const schema = { type: 'object', required: ['metric_crs'] };
    let state = reduceRuntimeEvent(projectionFromSnapshot(snapshot('a')), { run_id: 'a', seq: 11, event_id: 11, type: 'run.awaiting_input', payload: { patch_schema: schema, diagnostic: { code: 'CRS_REQUIRED' } } });
    expect(state.patch_schema).toEqual(schema); expect(state.diagnostic).toEqual({ code: 'CRS_REQUIRED' });
    state = reduceRuntimeEvent(state, { run_id: 'a', seq: 12, event_id: 12, type: 'run.status_changed', payload: { status: 'queued' } });
    expect(state.patch_schema).toBeNull(); expect(state.diagnostic).toBeNull();
  });
  it('rebases an invalid event cursor instead of retrying the rejected cursor forever', async () => {
    snapshots.set('cursor', snapshot('cursor')); streamCursorFailure = true;
    const { result } = renderHook(() => useRuntimeRun('cursor'));
    await waitFor(() => expect(requests.filter(item => item.url === 'GET /api/runs/cursor').length).toBe(2));
    expect(result.current.error).toBeNull();
    expect(result.current.projection?.run_id).toBe('cursor');
  });
});
describe('Runtime chat interactions', () => {
  it('continues streaming a second Run after the first one completes', async () => {
    nextSnapshots = [snapshot('first', 'completed'), snapshot('second')]; render(<ChatPanel sessionId="s" />);
    await screen.findByRole('button', { name: '发送' });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '第一个任务' } }); fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(false));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '第二个任务' } }); fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await screen.findByText('Run running'); expect(requests.some(item => item.url === 'GET /api/runs/second/events')).toBe(true);
  });
  it('restores a local paused Run after remount without creating another Run', async () => {
    restore(paused('paused')); render(<ChatPanel sessionId="s" />); await screen.findByText('等待你的回答');
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '使用 WGS84' } }); fireEvent.click(screen.getByRole('button', { name: '回答并继续' }));
    await waitFor(() => expect(requests.some(item => item.url === 'POST /api/runs/paused/resume')).toBe(true));
    expect(requests.some(item => item.url === 'POST /api/runs')).toBe(false);
  });
  it('recovers an active Run from the session server projection without local cache', async () => {
    const run = paused('server'); serverRuns = [run]; snapshots.set(run.run_id, run); render(<ChatPanel sessionId="s" />);
    await screen.findByText('等待你的回答'); expect(requests.some(item => item.url === 'GET /api/runs/server')).toBe(true);
  });
  it('submits typed schema fields instead of wrapping every answer in text', async () => {
    restore(paused('fields', { type: 'object', required: ['metric_crs', 'distance'], properties: { metric_crs: { type: 'string' }, distance: { type: 'number' } } })); render(<ChatPanel sessionId="s" />);
    fireEvent.change(await screen.findByLabelText('metric_crs'), { target: { value: 'EPSG:32650' } }); fireEvent.change(screen.getByLabelText('distance'), { target: { value: '500' } });
    fireEvent.click(screen.getByRole('button', { name: '提交补充信息' }));
    await waitFor(() => expect(requests.find(item => item.url === 'POST /api/runs/fields/resume')?.body.patch).toEqual({ metric_crs: 'EPSG:32650', distance: 500 }));
  });
  it('requires a click and submits the exact pending action hash for confirmation', async () => {
    const confirmation = { confirmation_id: 'c', task_id: 'publish-task', task_key: 'publish', action_hash: 'b'.repeat(64) };
    restore(snapshot('confirm', 'ready', { pending_confirmations: [confirmation] })); render(<ChatPanel sessionId="s" />);
    const button = await screen.findByRole('button', { name: '确认执行 publish' }); expect(requests.some(item => item.url === 'POST /api/runs/confirm/confirm')).toBe(false); fireEvent.click(button);
    await waitFor(() => expect(requests.find(item => item.url === 'POST /api/runs/confirm/confirm')?.body).toMatchObject({ task_id: 'publish-task', action_hash: confirmation.action_hash, confirmation: true, expected_run_version: 3 }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '确认执行 publish' })).toBeNull());
  });
  it('does not append a delayed map from the previous session to the current session', async () => {
    let resolveMap!: (value: unknown) => void; mapContent = new Promise(resolve => { resolveMap = resolve; });
    const crs = { kind: 'EPSG', code: 4326 };
    restore(snapshot('map', 'completed', { output_artifacts: [{ artifact_id: 'map-layer', type: 'MapLayer', crs }] })); const view = render(<ChatPanel sessionId="s" />);
    await waitFor(() => expect(requests.some(item => item.url.endsWith('/map-layer/content'))).toBe(true));
    localStorage.setItem('gismind.messages.other', JSON.stringify([{ id: 'other', role: 'assistant', blocks: [], status: 'done', created_at: 0 }])); view.rerender(<ChatPanel sessionId="other" />);
    await act(async () => { resolveMap({ crs, layers: [{ type: 'FeatureCollection', features: [
      { type: 'Feature', geometry: { type: 'Point', coordinates: [1, 2] }, properties: {} },
    ] }], bbox: [0, 0, 3, 3] }); }); expect(screen.queryByLabelText('地图')).toBeNull();
  });
  it('projects Runtime EPSG:3857 MapLayer coordinates before handing them to the map view', async () => {
    const crs = { kind: 'EPSG', code: 3857 };
    const x = 6378137 * 118.8 * Math.PI / 180;
    const y = 6378137 * Math.log(Math.tan(Math.PI / 4 + 32.05 * Math.PI / 360));
    mapContent = Promise.resolve({ crs, bbox: [x, y, x, y], layers: [{ type: 'FeatureCollection', features: [
      { type: 'Feature', geometry: { type: 'Point', coordinates: [x, y] }, properties: { name: '南京' } },
    ] }] });
    restore(snapshot('map', 'completed', { output_artifacts: [{ artifact_id: 'projected-layer', type: 'MapLayer', crs }] }));
    render(<ChatPanel sessionId="s" />);
    const layers = JSON.parse((await screen.findByLabelText('地图')).textContent!);
    expect(layers[0].features[0].geometry.coordinates[0]).toBeCloseTo(118.80517716491906, 8);
    expect(layers[0].features[0].geometry.coordinates[1]).toBeCloseTo(32.04792177186948, 8);
  });
  it('shows a missing CRS error instead of rendering unlabelled Runtime coordinates', async () => {
    mapContent = Promise.resolve({ bbox: [13000000, 3700000, 13000000, 3700000], layers: [{ type: 'FeatureCollection', features: [
      { type: 'Feature', geometry: { type: 'Point', coordinates: [13000000, 3700000] }, properties: {} },
    ] }] });
    restore(snapshot('map', 'completed', { output_artifacts: [{ artifact_id: 'unlabelled-layer', type: 'MapLayer' }] }));
    render(<ChatPanel sessionId="s" />);
    await screen.findByText(/地图加载失败：.*CRS/);
    expect(screen.queryByLabelText('地图')).toBeNull();
  });
  it('keeps an unresolved cancellation paused until an explicit retry decision', async () => {
    keepAwaiting = true;
    restore(snapshot('decision', 'awaiting_input', { awaiting_input: { kind: 'cancellation_decision_required', task_ids: ['side-effect'] } }));
    render(<ChatPanel sessionId="s" />); await screen.findByText('等待你的回答');
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '稍后再说' } }); fireEvent.click(screen.getByRole('button', { name: '回答并继续' }));
    await waitFor(() => expect(requests.filter(item => item.url === 'GET /api/runs/decision').length).toBe(2));
    expect(screen.getByText('等待你的回答')).toBeTruthy();
    expect(requests.find(item => item.url === 'POST /api/runs/decision/resume')?.body.patch).toEqual({ answer: '稍后再说' });
    keepAwaiting = false;
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '重试' } }); fireEvent.click(screen.getByRole('button', { name: '回答并继续' }));
    await waitFor(() => expect(requests.filter(item => item.url === 'POST /api/runs/decision/resume').at(-1)?.body.patch).toEqual({ answer: '重试', task_decisions: { 'side-effect': 'retry' } }));
  });
  it('shows cancellation failure and keeps the active Run attached', async () => {
    restore(snapshot('cancel')); rejectCancel = true; render(<ChatPanel sessionId="s" />);
    await screen.findByText('Run running'); fireEvent.click(screen.getByRole('button', { name: '停止' }));
    await screen.findByRole('alert');
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(true);
    expect(JSON.parse(localStorage.getItem('gismind.active_run.s') ?? '{}').runId).toBe('cancel');
    await waitFor(() => expect(requests.filter(item => item.url === 'GET /api/runs/cancel').length).toBe(2));
  });
});
