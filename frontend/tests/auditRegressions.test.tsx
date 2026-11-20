import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { getRuntimeRun, RuntimeApiError } from '@/api/client';
import { ArtifactMapView } from '@/components/ArtifactMapView';
import { ArtifactWorkspace } from '@/components/ArtifactWorkspace';
import { ChatPanel } from '@/components/ChatPanel';
import { FullscreenMap } from '@/components/FullscreenMap';
import { RuntimeInputForm } from '@/components/RuntimeInputForm';
import { SessionCard } from '@/components/SessionCard';
import { SessionRenameInput } from '@/components/SessionRenameInput';
import { ThemeToggle } from '@/components/ThemeToggle';
import { useRuntimeRun } from '@/hooks/useRuntimeRun';
import { useSessions } from '@/hooks/useSessions';
import { useTheme } from '@/hooks/useTheme';
import { featureToOverlay, renderLayersOnMap, resetSharedInfoWindow, type AMapMapLike, type AMapPolygonOptions, type AMapSdkLike } from '@/lib/mapRenderers';
import type { MapLayer } from '@/types/message';
import type { RunSnapshot, RuntimeEvent } from '@/types/runtime';

const { loadAMapMock, lazyMapRenderSpy } = vi.hoisted(() => ({
  loadAMapMock: vi.fn(),
  lazyMapRenderSpy: vi.fn(),
}));

vi.mock('@/lib/amap', () => ({ loadAMap: loadAMapMock }));
vi.mock('@/components/LazyMapView', () => ({
  LazyMapView: ({ layers, sourceArtifactId }: { layers: unknown; sourceArtifactId?: string }) => {
    lazyMapRenderSpy({ layers, sourceArtifactId });
    return <div aria-label="测试地图" data-source-artifact-id={sourceArtifactId ?? ''}>{JSON.stringify(layers)}</div>;
  },
}));

const encoder = new TextEncoder();
const wgs84 = { kind: 'EPSG', code: 4326 };

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
}

function sseResponse(events: RuntimeEvent[]): Response {
  const payload = events.map((ev) => `id: ${ev.event_id}\ndata: ${JSON.stringify(ev)}\n\n`).join('');
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(payload));
      controller.close();
    },
  }), { headers: { 'Content-Type': 'text/event-stream' } });
}

beforeEach(() => {
  localStorage.clear();
  document.documentElement.className = 'dark';
  lazyMapRenderSpy.mockReset();
  loadAMapMock.mockReset();
  HTMLElement.prototype.scrollTo = vi.fn();
  vi.stubGlobal('IntersectionObserver', class { observe() {} unobserve() {} disconnect() {} });
});

afterEach(() => {
  cleanup();
  resetSharedInfoWindow();
  vi.unstubAllGlobals();
});

describe('API error envelope & session/UI shape alignment', () => {
  it('preserves details and trace_id on RuntimeApiError', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({
      error: {
        code: 'RUN_NOT_FOUND',
        message: 'run does not exist',
        details: { run_id: 'missing-1' },
        trace_id: 'trace-xyz-99',
      },
    }, 404)));

    await expect(getRuntimeRun('missing-1')).rejects.toMatchObject({
      name: 'RuntimeApiError',
      code: 'RUN_NOT_FOUND',
      status: 404,
      details: { run_id: 'missing-1' },
      trace_id: 'trace-xyz-99',
    });
  });

  it('renders SessionCard without undefined tools when tool_count is absent and avoids nested buttons', () => {
    const onActivate = vi.fn();
    const onRename = vi.fn().mockRejectedValue(new Error('rename failed'));
    const onDelete = vi.fn().mockRejectedValue(new Error('delete failed'));
    vi.stubGlobal('confirm', vi.fn(() => true));

    const { container } = render(
      <SessionCard
        meta={{ id: 'sess-abcd', title: 'Minimal Session', created_at: Date.now(), updated_at: Date.now() }}
        active={false}
        onActivate={onActivate}
        onRename={onRename}
        onDelete={onDelete}
      />,
    );

    expect(container.textContent).not.toContain('undefined');
    expect(container.textContent).not.toContain('tools');
    // No button should be nested inside another button or role="button"
    const buttons = Array.from(container.querySelectorAll('button'));
    expect(buttons.length).toBe(3);
    for (const btn of buttons) {
      expect(btn.parentElement?.closest('button, [role="button"]')).toBeNull();
    }
    fireEvent.click(screen.getByRole('button', { name: 'Delete dispatch' }));
    expect(onDelete).toHaveBeenCalledOnce();
  });

  it('uses canonical CRS object placeholder in RuntimeInputForm', () => {
    render(
      <RuntimeInputForm
        disabled={false}
        onSubmit={vi.fn()}
        schema={{ properties: { crs: { type: 'object' } } }}
      />,
    );
    const textarea = screen.getByRole('textbox', { name: 'crs' }) as HTMLTextAreaElement;
    expect(textarea.placeholder).toContain('{"kind":"EPSG","code":4326}');
    expect(textarea.placeholder).not.toContain('{"epsg":4326}');
  });

  it('clears gismind.active_run.<id> when deleting a session', async () => {
    localStorage.setItem('gismind.active_session', 's1');
    localStorage.setItem('gismind.messages.s1', '[]');
    localStorage.setItem('gismind.active_run.s1', JSON.stringify({ runId: 'r1', messageId: 'm1' }));
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'DELETE') return new Response(null, { status: 204 });
      return json({
        items: [
          { id: 's1', title: 'One', created_at: 2, updated_at: 2 },
          { id: 's2', title: 'Two', created_at: 1, updated_at: 1 },
        ],
      });
    }));

    const { result } = renderHook(() => useSessions());
    await waitFor(() => expect(result.current.activeId).toBe('s1'));
    await act(async () => { await result.current.remove('s1'); });
    expect(localStorage.getItem('gismind.messages.s1')).toBeNull();
    expect(localStorage.getItem('gismind.active_run.s1')).toBeNull();
  });
});

describe('Stale localStorage activeRun unlock, fast MapLayer settlement & cancelled state', () => {
  it('unlocks composer and clears stale activeRun on 404 RUN_NOT_FOUND', async () => {
    localStorage.setItem('gismind.active_run.s', JSON.stringify({ runId: 'stale-404', messageId: 'assistant_stale' }));
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/sessions/s/messages') return json({ messages: [] });
      if (url === '/api/sessions/s/runs') return json({ items: [] });
      if (url === '/api/runs/stale-404') return json({ error: { code: 'RUN_NOT_FOUND', message: 'not found' } }, 404);
      throw new Error(`Unexpected ${url}`);
    }));

    render(<ChatPanel sessionId="s" />);
    await waitFor(() => expect(localStorage.getItem('gismind.active_run.s')).toBeNull());
    await waitFor(() => expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(false));
    expect(screen.getByRole('button', { name: '发送' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '停止' })).toBeNull();
    expect(screen.getByRole('alert').textContent).toContain('上次运行记录已不存在');
  });

  it('hydrates a MapLayer discovered for the first time during terminal settlement', async () => {
    let eventController: ReadableStreamDefaultController<Uint8Array> | undefined;
    let settled = false;
    const mapArtifact = { artifact_id: 'art_fast_map', type: 'MapLayer', crs: wgs84 };
    const mapPayload = {
      crs: wgs84,
      bbox: [118.7, 32.0, 118.9, 32.1],
      layers: [{
        type: 'FeatureCollection',
        features: [{ type: 'Feature', geometry: { type: 'Point', coordinates: [118.8, 32.05] }, properties: {} }],
      }],
    };
    const runningSnapshot: RunSnapshot = {
      run_id: 'fast-run', session_id: 's', status: 'running', run_version: 1,
      tasks: [], output_artifacts: [], last_event_id: 1, last_seq: 1,
    };
    const completedSnapshot: RunSnapshot = {
      ...runningSnapshot, status: 'completed', run_version: 2,
      output_artifacts: [mapArtifact], final_artifact_ids: ['art_fast_map'], last_event_id: 2, last_seq: 2,
    };

    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/api/sessions/s/messages') return json({ messages: [] });
      if (url === '/api/sessions/s/runs') return json({ items: [{ run_id: 'fast-run', session_id: 's', status: 'running', run_version: 1 }] });
      if (url === '/api/runs/fast-run') return json(settled ? completedSnapshot : runningSnapshot);
      if (url === '/api/runs/fast-run/events') {
        return new Response(new ReadableStream<Uint8Array>({
          start(controller) {
            eventController = controller;
            init?.signal?.addEventListener('abort', () => { try { controller.close(); } catch { /* noop */ } });
          },
        }));
      }
      if (url === '/api/artifacts/art_fast_map') return json(mapArtifact);
      if (url === '/api/artifacts/art_fast_map/content') return json(mapPayload);
      throw new Error(`Unexpected ${url}`);
    }));

    render(<ChatPanel sessionId="s" />);
    await waitFor(() => expect(eventController).toBeDefined());
    settled = true;
    await act(async () => {
      const doneEvent: RuntimeEvent = {
        event_id: 2, seq: 2, run_id: 'fast-run', type: 'run.completed', payload: { status: 'completed' },
      };
      eventController!.enqueue(encoder.encode(`id: 2\ndata: ${JSON.stringify(doneEvent)}\n\n`));
    });

    await waitFor(() => expect(localStorage.getItem('gismind.active_run.s')).toBeNull());
    await waitFor(() => expect(screen.getAllByLabelText('测试地图').length).toBeGreaterThanOrEqual(1));
    expect(screen.queryByRole('button', { name: '加载历史地图' })).toBeNull();
  });

  it('renders a readable cancelled state instead of a blank bubble when cancelled with no blocks', async () => {
    localStorage.setItem('gismind.active_run.s', JSON.stringify({ runId: 'cancelled-run', messageId: 'assistant_cancelled' }));
    const cancelledSnapshot: RunSnapshot = {
      run_id: 'cancelled-run', session_id: 's', status: 'cancelled', run_version: 2,
      tasks: [], output_artifacts: [], last_event_id: 3, last_seq: 3,
    };
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/sessions/s/messages') return json({ messages: [] });
      if (url === '/api/sessions/s/runs') return json({ items: [] });
      if (url === '/api/runs/cancelled-run') return json(cancelledSnapshot);
      throw new Error(`Unexpected ${url}`);
    }));

    render(<ChatPanel sessionId="s" />);
    await screen.findByText('RUN_CANCELLED');
    expect(screen.getByText('任务已取消')).toBeTruthy();
  });

  it('reuses an idempotent command_id across retried cancel commands until accepted or version changes', async () => {
    const cancelBodies: Array<Record<string, unknown>> = [];
    let failFirst = true;
    const runningSnapshot: RunSnapshot = {
      run_id: 'retry-cmd', session_id: 's', status: 'running', run_version: 5,
      tasks: [], output_artifacts: [], last_event_id: 1, last_seq: 1,
    };
    localStorage.setItem('gismind.active_run.s', JSON.stringify({ runId: 'retry-cmd', messageId: 'assistant_retry' }));

    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/api/sessions/s/messages') return json({ messages: [] });
      if (url === '/api/sessions/s/runs') return json({ items: [] });
      if (url === '/api/runs/retry-cmd') return json(runningSnapshot);
      if (url === '/api/runs/retry-cmd/events') {
        return new Response(new ReadableStream<Uint8Array>({
          start(controller) {
            init?.signal?.addEventListener('abort', () => { try { controller.close(); } catch { /* noop */ } });
          },
        }));
      }
      if (url === '/api/runs/retry-cmd/cancel' && init?.method === 'POST') {
        cancelBodies.push(JSON.parse(String(init.body)));
        if (failFirst) {
          failFirst = false;
          return json({ error: { code: 'TEMPORARY_FAILURE', message: 'retry cancel' } }, 503);
        }
        return json({ run_id: 'retry-cmd', status: 'cancelling', run_version: 6 });
      }
      throw new Error(`Unexpected ${url}`);
    }));

    render(<ChatPanel sessionId="s" />);
    await screen.findByText('Run running');
    const stopBtn = screen.getByRole('button', { name: '停止' });
    fireEvent.click(stopBtn);
    await screen.findByText(/retry cancel/);
    fireEvent.click(screen.getByRole('button', { name: '停止' }));
    await waitFor(() => expect(cancelBodies).toHaveLength(2));
    expect(cancelBodies[1].command_id).toBe(cancelBodies[0].command_id);
  });

  it('does not re-render LazyMapView while typing in the composer', async () => {
    const mapArtifact = { artifact_id: 'art_stable', type: 'MapLayer', crs: wgs84 };
    const mapPayload = {
      crs: wgs84,
      bbox: [118.7, 32.0, 118.9, 32.1],
      layers: [{
        type: 'FeatureCollection',
        features: [{ type: 'Feature', geometry: { type: 'Point', coordinates: [118.8, 32.05] }, properties: {} }],
      }],
    };
    const completed: RunSnapshot = {
      run_id: 'done-run', session_id: 's', status: 'completed', run_version: 1,
      tasks: [], output_artifacts: [mapArtifact], final_artifact_ids: ['art_stable'], last_event_id: 2, last_seq: 2,
    };

    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/sessions/s/messages') return json({ messages: [] });
      if (url === '/api/sessions/s/runs') return json({ items: [completed] });
      if (url === '/api/runs/done-run') return json(completed);
      if (url === '/api/artifacts/art_stable') return json(mapArtifact);
      if (url === '/api/artifacts/art_stable/content') return json(mapPayload);
      throw new Error(`Unexpected ${url}`);
    }));

    render(<ChatPanel sessionId="s" />);
    await screen.findByLabelText('测试地图');
    const rendersBeforeTyping = lazyMapRenderSpy.mock.calls.length;
    const textbox = screen.getByRole('textbox') as HTMLTextAreaElement;
    fireEvent.change(textbox, { target: { value: 'a' } });
    fireEvent.change(textbox, { target: { value: 'ab' } });
    fireEvent.change(textbox, { target: { value: 'abc' } });
    expect(lazyMapRenderSpy.mock.calls.length).toBe(rendersBeforeTyping);
  });
});

describe('useRuntimeRun initial 5xx retry, checkpoint event preservation, and scoped error', () => {
  it('retries transient initial 5xx snapshot failures and recovers automatically', async () => {
    let attempts = 0;
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.endsWith('/runs/flaky')) {
        attempts += 1;
        if (attempts === 1) return json({ error: { code: 'TEMPORARY_FAILURE', message: 'db busy' } }, 503);
        return json({
          run_id: 'flaky', session_id: 's', status: 'completed', run_version: 2,
          tasks: [], output_artifacts: [], last_event_id: 10, last_seq: 10,
        });
      }
      throw new Error(`Unexpected ${url}`);
    }));

    const { result } = renderHook(() => useRuntimeRun('flaky'));
    await waitFor(() => expect(result.current.terminalSettled).toBe(true), { timeout: 2500 });
    expect(attempts).toBe(2);
    expect(result.current.error).toBeNull();
    expect(result.current.projection?.status).toBe('completed');
  });

  it('scopes error to runId and fails fast on 404 RUN_NOT_FOUND without retrying', async () => {
    let fetches = 0;
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      fetches += 1;
      if (url.endsWith('/runs/missing')) {
        return json({ error: { code: 'RUN_NOT_FOUND', message: 'gone' } }, 404);
      }
      return json({
        run_id: 'ok-run', session_id: 's', status: 'completed', run_version: 1,
        tasks: [], output_artifacts: [], last_event_id: 1, last_seq: 1,
      });
    }));

    const { result, rerender } = renderHook(({ id }) => useRuntimeRun(id), { initialProps: { id: 'missing' as string | null } });
    await waitFor(() => expect(result.current.error).toBeInstanceOf(RuntimeApiError));
    expect(result.current.connectionState).toBe('closed');
    expect(fetches).toBe(1);

    rerender({ id: 'ok-run' });
    expect(result.current.error).toBeNull();
    await waitFor(() => expect(result.current.terminalSettled).toBe(true));
  });

  it('preserves intermediate SSE frames when a checkpoint refresh returns a newer last_seq and refreshes even with empty tasks', async () => {
    let snapshots = 0;
    const task = { task_id: 't1', task_key: 'step1', status: 'running', attempts: 1 };
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.endsWith('/runs/chk')) {
        snapshots += 1;
        if (snapshots === 1) {
          return json({
            run_id: 'chk', session_id: 's', status: 'planning', run_version: 1,
            tasks: [], output_artifacts: [], last_event_id: 100, last_seq: 1,
          });
        }
        // Checkpoint snapshot is already ahead at seq 4!
        return json({
          run_id: 'chk', session_id: 's', status: 'running', run_version: 2,
          tasks: [task], output_artifacts: [], last_event_id: 104, last_seq: 4,
        });
      }
      if (url.endsWith('/runs/chk/events')) {
        return sseResponse([
          { run_id: 'chk', event_id: 102, seq: 2, type: 'run.ready', payload: { status: 'ready' } },
          { run_id: 'chk', event_id: 103, seq: 3, type: 'run.thought', payload: { message: 'intermediate' } },
          { run_id: 'chk', event_id: 104, seq: 4, type: 'run.completed', payload: { status: 'completed' } },
        ]);
      }
      throw new Error(`Unexpected ${url}`);
    }));

    const { result } = renderHook(() => useRuntimeRun('chk'));
    await waitFor(() => expect(result.current.terminalSettled).toBe(true));
    expect(snapshots).toBeGreaterThanOrEqual(2);
    expect(result.current.projection?.events.map((e) => e.seq)).toEqual([2, 3, 4]);
    expect(result.current.projection?.tasks).toHaveLength(1);
  });

  it('preserves prior projection.events across same-run connect() reconnects', async () => {
    let streams = 0;
    let firstController: ReadableStreamDefaultController<Uint8Array> | undefined;
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/runs/same')) {
        return json({
          run_id: 'same', session_id: 's', status: 'running', run_version: streams === 0 ? 1 : 2,
          tasks: [], output_artifacts: [], last_event_id: streams === 0 ? 10 : 11, last_seq: streams === 0 ? 1 : 2,
        });
      }
      if (url.endsWith('/runs/same/events')) {
        streams += 1;
        if (streams === 1) {
          return new Response(new ReadableStream<Uint8Array>({
            start(controller) {
              firstController = controller;
              controller.enqueue(encoder.encode(`id: 11\ndata: ${JSON.stringify({ run_id: 'same', event_id: 11, seq: 2, type: 'run.thought', payload: { message: 'before resume' } })}\n\n`));
              init?.signal?.addEventListener('abort', () => { try { controller.close(); } catch { /* noop */ } });
            },
          }), { headers: { 'Content-Type': 'text/event-stream' } });
        }
        return sseResponse([
          { run_id: 'same', event_id: 12, seq: 3, type: 'run.completed', payload: { status: 'completed' } },
        ]);
      }
      throw new Error(`Unexpected ${url}`);
    }));

    const { result } = renderHook(() => useRuntimeRun('same'));
    await waitFor(() => expect(result.current.projection?.events.map((e) => e.seq)).toEqual([2]));
    expect(firstController).toBeDefined();
    await act(async () => { await result.current.connect(); });
    await waitFor(() => expect(result.current.terminalSettled).toBe(true));
    expect(result.current.projection?.events.map((e) => e.seq)).toEqual([2, 3]);
  });
});

describe('Theme synchronization, IME & A11y fixes', () => {
  it('synchronizes theme across multiple useTheme callers and toggles dark/light mutually exclusively', () => {
    function Harness() {
      const a = useTheme();
      const b = useTheme();
      return (
        <div>
          <span data-testid="theme-a">{a.theme}</span>
          <span data-testid="theme-b">{b.theme}</span>
          <button type="button" onClick={a.toggle}>toggle</button>
        </div>
      );
    }
    render(<Harness />);
    expect(screen.getByTestId('theme-a').textContent).toBe('dark');
    expect(screen.getByTestId('theme-b').textContent).toBe('dark');
    expect(document.documentElement.classList.contains('dark')).toBe(true);
    expect(document.documentElement.classList.contains('light')).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'toggle' }));
    expect(screen.getByTestId('theme-a').textContent).toBe('light');
    expect(screen.getByTestId('theme-b').textContent).toBe('light');
    expect(document.documentElement.classList.contains('light')).toBe(true);
    expect(document.documentElement.classList.contains('dark')).toBe(false);
  });

  it('ignores t shortcut inside <select> and during IME composition', () => {
    const onChange = vi.fn();
    render(
      <div>
        <ThemeToggle theme="dark" onChange={onChange} />
        <select aria-label="crs-select"><option value="a">A</option></select>
      </div>,
    );
    const select = screen.getByLabelText('crs-select');
    fireEvent.keyDown(select, { key: 't' });
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.keyDown(window, { key: 't', isComposing: true });
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.keyDown(window, { key: 't' });
    expect(onChange).toHaveBeenCalledWith('light');
  });

  it('ignores IME composing Enter in SessionRenameInput and prevents duplicate commit on Enter + blur', () => {
    const onCommit = vi.fn();
    const onCancel = vi.fn();
    render(<SessionRenameInput initialTitle="Old Title" onCommit={onCommit} onCancel={onCancel} />);
    const input = screen.getByLabelText('Rename session') as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'New Title' } });

    const composingEvent = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
    Object.defineProperty(composingEvent, 'isComposing', { value: true });
    input.dispatchEvent(composingEvent);
    expect(onCommit).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: 'Enter' });
    fireEvent.blur(input);
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCommit).toHaveBeenCalledWith('New Title');
  });
});

describe('Map renderer, FullscreenMap stability, and Artifact downloads', () => {
  it('passes strokeWeight (not strokeWidth) for Polygon layers and builds popups only for Point features', () => {
    const polygonOptions: AMapPolygonOptions[] = [];
    const infoWindowCreates: Array<{ content: string }> = [];
    const mockMap: AMapMapLike = { setBounds: vi.fn() };
    const mockSdk: AMapSdkLike = {
      Marker: class {
        map: AMapMapLike | null = null;
        pos: [number, number];
        clickHandler?: () => void;
        constructor(opts: { position: [number, number] }) { this.pos = opts.position; }
        setMap(map: AMapMapLike | null) { this.map = map; }
        getMap() { return this.map; }
        getPosition() { return this.pos; }
        on(_ev: string, fn: () => void) { this.clickHandler = fn; }
      },
      Polyline: class { setMap() {} },
      Polygon: class {
        constructor(opts: AMapPolygonOptions) { polygonOptions.push(opts); }
        setMap() {}
      },
      ImageOverlay: class { setMap() {} },
      InfoWindow: class {
        constructor(opts: { content: string }) { infoWindowCreates.push(opts); }
        setContent() {}
        open() {}
        close() {}
      },
      Pixel: class {},
      LngLat: class {},
      Bounds: class {},
    };

    const layer: MapLayer = {
      type: 'polygon',
      coordinates: [[[[118, 32], [119, 32], [119, 33], [118, 32]]]],
      style: { stroke_width: 3.5, stroke_color: '#00ff00' },
    };
    renderLayersOnMap(mockMap, mockSdk, [layer], [118, 32, 119, 33]);
    expect(polygonOptions).toHaveLength(1);
    expect(polygonOptions[0].strokeWeight).toBe(3.5);
    expect('strokeWidth' in (polygonOptions[0] as unknown as Record<string, unknown>)).toBe(false);

    // Polygon feature properties should not need InfoWindow creation
    const polyOverlay = featureToOverlay(mockSdk, {
      type: 'Feature',
      geometry: { type: 'Polygon', coordinates: [[[118, 32], [119, 32], [119, 33], [118, 32]]] },
      properties: { name: 'area' },
    });
    expect(polyOverlay).not.toBeNull();
    expect(infoWindowCreates).toHaveLength(0);
  });

  it('keeps FullscreenMap AMap instance alive across identical layers/bbox re-renders and exposes dialog a11y + focus restore', async () => {
    const mapInstances: Array<{ destroyed: boolean }> = [];
    const SDK = {
      Map: class {
        destroyed = false;
        constructor() { mapInstances.push(this); }
        addControl() {}
        setBounds() {}
        destroy() { this.destroyed = true; }
      },
      Marker: class { setMap() {} on() {} getMap() { return null; } getPosition() { return [0, 0]; } },
      Pixel: class {},
      Bounds: class {},
      Scale: class {},
      ToolBar: class {},
    };
    loadAMapMock.mockResolvedValue(SDK);

    const trigger = document.createElement('button');
    document.body.appendChild(trigger);
    trigger.focus();

    const makeLayers = (): MapLayer[] => [{
      type: 'FeatureCollection',
      features: [{ type: 'Feature', geometry: { type: 'Point', coordinates: [118.8, 32.05] } }],
    }];
    const makeBbox = (): [number, number, number, number] => [118.7, 32.0, 118.9, 32.1];

    const view = render(<FullscreenMap layers={makeLayers()} bbox={makeBbox()} onClose={vi.fn()} />);
    await waitFor(() => expect(mapInstances).toHaveLength(1));
    const dialog = screen.getByRole('dialog', { name: '全屏空间分析地图' });
    expect(dialog.getAttribute('aria-modal')).toBe('true');

    // Rerender with fresh object references but identical values
    view.rerender(<FullscreenMap layers={makeLayers()} bbox={makeBbox()} onClose={vi.fn()} />);
    await act(async () => { await Promise.resolve(); });
    expect(mapInstances).toHaveLength(1);
    expect(mapInstances[0].destroyed).toBe(false);

    view.unmount();
    expect(document.activeElement).toBe(trigger);
    trigger.remove();
  });

  it('assigns sensible download filenames with extensions for Vector, MapLayer, and POISet in ArtifactWorkspace', async () => {
    const vectorItem = { artifact_id: 'art_vec', type: 'Vector', crs: wgs84, media_type: 'application/geo+json', validation_status: 'valid' };
    const mapItem = { artifact_id: 'art_map', type: 'MapLayer', crs: wgs84, media_type: 'application/json', validation_status: 'valid' };
    const poiItem = { artifact_id: 'art_poi', type: 'POISet', crs: wgs84, media_type: 'application/json', validation_status: 'valid' };
    const items = new Map([
      ['art_vec', vectorItem],
      ['art_map', mapItem],
      ['art_poi', poiItem],
    ]);
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      const match = url.match(/^\/api\/artifacts\/([^/]+)(\/content)?$/);
      if (!match) throw new Error(`Unexpected ${url}`);
      if (match[2]) return json({ type: 'FeatureCollection', features: [] });
      return json(items.get(match[1]));
    }));

    render(<ArtifactWorkspace artifacts={[vectorItem, mapItem, poiItem]} onUse={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('link', { name: '下载原始成果' }).getAttribute('download')).toBe('art_vec.geojson'));

    fireEvent.click(screen.getByRole('button', { name: '选择成果 art_map' }));
    await waitFor(() => expect(screen.getByRole('link', { name: '下载原始成果' }).getAttribute('download')).toBe('art_map.json'));

    fireEvent.click(screen.getByRole('button', { name: '选择成果 art_poi' }));
    await waitFor(() => expect(screen.getByRole('link', { name: '下载原始成果' }).getAttribute('download')).toBe('art_poi.json'));
  });

  it('verifies metadata type and passes sourceArtifactId in ArtifactMapView', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/artifacts/art_bad') return json({ artifact_id: 'art_bad', type: 'Vector', crs: wgs84 });
      throw new Error(`Unexpected ${url}`);
    }));

    render(
      <ArtifactMapView
        block={{ type: 'map', artifact_id: 'art_bad', expired: true, layers: [], bbox: [0, 0, 1, 1] }}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '加载历史地图' }));
    expect((await screen.findByRole('alert')).textContent).toContain('地图成果标识或类型与服务端响应不一致');
  });
});
