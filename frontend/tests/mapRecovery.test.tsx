import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LazyMapView } from '@/components/LazyMapView';
import { FullscreenMap } from '@/components/FullscreenMap';
import type { MapLayer } from '@/types/message';

const { loadAMap } = vi.hoisted(() => ({ loadAMap: vi.fn() }));
vi.mock('@/lib/amap', () => ({ loadAMap }));

let visibility: (entries: Array<{ isIntersecting: boolean }>) => void;
let controlFailure = false;
const maps: SDKMap[] = [];

// Offline SDK boundary: components and their geometry renderer remain real.
class SDKMap {
  overlays: SDKMarker[] = [];
  destroyed = false;
  constructor() { maps.push(this); }
  addControl() { if (controlFailure) throw new Error('control initialization failed'); }
  getAllOverlays(type: string) { return type === 'marker' ? this.overlays : []; }
  setBounds() {}
  destroy() {
    this.destroyed = true;
    this.overlays.forEach(overlay => { overlay.map = null; });
    this.overlays = [];
  }
}
class SDKMarker {
  map: SDKMap | null = null;
  constructor(public options: { position: number[] }) {}
  setMap(map: SDKMap | null) {
    if (this.map) this.map.overlays = this.map.overlays.filter(item => item !== this);
    this.map = map;
    if (map) map.overlays.push(this);
  }
  getMap() { return this.map; }
  getPosition() { return this.options.position; }
  on() {}
}
const SDK = { Map: SDKMap, Marker: SDKMarker, Pixel: class {}, Bounds: class {}, Scale: class {}, ToolBar: class {} };
const layers: MapLayer[] = [{ type: 'FeatureCollection', features: [
  { type: 'Feature', geometry: { type: 'Point', coordinates: [118.8, 32.05] } },
] }];
const bbox: [number, number, number, number] = [118.7, 32, 119.1, 32.2];

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise; reject = rejectPromise;
  });
  return { promise, resolve, reject };
}
function showMap() { act(() => visibility([{ isIntersecting: true }])); }
function expectNoSuccess(container: HTMLElement) {
  expect(screen.queryByRole('button', { name: '全屏展开地图' })).toBeNull();
  expect(container.querySelector('[data-map-render-evidence]')).toBeNull();
}

beforeEach(() => {
  maps.length = 0; controlFailure = false;
  loadAMap.mockReset().mockResolvedValue(SDK);
  vi.stubGlobal('fetch', vi.fn(() => { throw new Error('Unexpected network request'); }));
  vi.stubGlobal('IntersectionObserver', class {
    constructor(callback: typeof visibility) { visibility = callback; }
    observe() {} disconnect() {}
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('embedded map recovery', () => {
  it('retries a failed SDK load locally and blocks repeated clicks while pending', async () => {
    const retry = deferred<typeof SDK>();
    loadAMap.mockRejectedValueOnce(new Error('network closed')).mockReturnValueOnce(retry.promise);
    const submit = vi.fn((event: React.FormEvent) => event.preventDefault());
    const view = render(<form onSubmit={submit}><LazyMapView layers={layers} bbox={bbox} sourceArtifactId="recovered" /></form>);
    showMap();
    expect((await screen.findByRole('alert')).textContent).toContain('network closed');
    expectNoSuccess(view.container);
    fireEvent.click(screen.getByRole('button', { name: '重试加载地图' }));
    const pendingButton = screen.getByRole('button', { name: '重试加载地图' }) as HTMLButtonElement;
    expect(pendingButton.disabled).toBe(true);
    fireEvent.click(pendingButton);
    expect(loadAMap).toHaveBeenCalledTimes(2);
    expectNoSuccess(view.container);
    await act(async () => retry.resolve(SDK));
    await screen.findByRole('button', { name: '全屏展开地图' });
    const observed = JSON.parse(view.container.querySelector('[data-map-render-evidence]')!.getAttribute('data-map-render-evidence')!);
    expect(observed).toMatchObject({ sourceArtifactId: 'recovered', overlayCount: 1, bounds: [118.8, 32.05, 118.8, 32.05] });
    expect(screen.queryByRole('alert')).toBeNull();
    expect(submit).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each([null, 'network unavailable'])('shows a retryable failure for unknown rejection %s', async rejection => {
    loadAMap.mockRejectedValueOnce(rejection);
    const view = render(<LazyMapView layers={layers} bbox={bbox} />);
    showMap();
    expect((await screen.findByRole('alert')).textContent).toContain('地图加载失败');
    expect(screen.getByRole('button', { name: '重试加载地图' })).toBeTruthy();
    expectNoSuccess(view.container);
  });

  it('destroys a partly rendered map before retrying initialization', async () => {
    const firstLoad = deferred<typeof SDK>();
    loadAMap.mockReturnValueOnce(firstLoad.promise);
    let failFeatureRead = false;
    const unstableLayer: MapLayer = {
      type: 'FeatureCollection',
      get features() {
        if (failFeatureRead) { failFeatureRead = false; throw new Error('layer read interrupted'); }
        return [];
      },
    };
    const view = render(<LazyMapView layers={[...layers, unstableLayer]} bbox={bbox} sourceArtifactId="partial-map" />);
    showMap();
    failFeatureRead = true;
    await act(async () => firstLoad.resolve(SDK));
    expect((await screen.findByRole('alert')).textContent).toContain('layer read interrupted');
    expect(maps[0].destroyed).toBe(true);
    expect(maps[0].overlays).toHaveLength(0);
    expectNoSuccess(view.container);
    fireEvent.click(screen.getByRole('button', { name: '重试加载地图' }));
    await screen.findByRole('button', { name: '全屏展开地图' });
    expect(maps).toHaveLength(2);
    expect(maps[1].overlays).toHaveLength(1);
  });

  it('clears prior evidence when a resumed map fails and while retrying', async () => {
    const view = render(<LazyMapView layers={layers} bbox={bbox} sourceArtifactId="previous" />);
    showMap();
    await screen.findByRole('button', { name: '全屏展开地图' });
    expect(view.container.querySelector('[data-map-render-evidence]')).not.toBeNull();
    act(() => visibility([{ isIntersecting: false }]));
    loadAMap.mockRejectedValueOnce(new Error('resume failed'));
    showMap();
    await screen.findByRole('alert');
    expectNoSuccess(view.container);
    const retry = deferred<typeof SDK>();
    loadAMap.mockReturnValueOnce(retry.promise);
    fireEvent.click(screen.getByRole('button', { name: '重试加载地图' }));
    expectNoSuccess(view.container);
    await act(async () => retry.resolve(SDK));
    await screen.findByRole('button', { name: '全屏展开地图' });
  });

  it('ignores a late SDK resolution during sleep and loads only after becoming visible again', async () => {
    const initial = deferred<typeof SDK>();
    loadAMap.mockReturnValueOnce(initial.promise);
    const view = render(<LazyMapView layers={layers} bbox={bbox} sourceArtifactId="sleeping" />);
    showMap();
    act(() => visibility([{ isIntersecting: false }]));
    await act(async () => initial.resolve(SDK));
    expect(maps).toHaveLength(0);
    expectNoSuccess(view.container);
    expect(screen.getByText('地图已休眠，滚动至此加载')).toBeTruthy();
    showMap();
    await screen.findByRole('button', { name: '全屏展开地图' });
    expect(maps).toHaveLength(1);
  });

  it('does not create a map after unmounting during SDK loading', async () => {
    const initial = deferred<typeof SDK>();
    loadAMap.mockReturnValueOnce(initial.promise);
    const view = render(<LazyMapView layers={layers} bbox={bbox} />);
    showMap(); view.unmount();
    await act(async () => initial.resolve(SDK));
    expect(maps).toHaveLength(0);
    expect(document.querySelector('[data-map-render-evidence]')).toBeNull();
  });
});

describe('fullscreen map recovery', () => {
  it('cleans up a candidate whose control initialization failed and retries with the same SDK', async () => {
    controlFailure = true;
    render(<FullscreenMap layers={layers} bbox={bbox} onClose={() => {}} />);
    expect((await screen.findByRole('alert')).textContent).toContain('control initialization failed');
    expect(maps[0].destroyed).toBe(true);
    controlFailure = false;
    const retry = deferred<typeof SDK>();
    loadAMap.mockReturnValueOnce(retry.promise);
    fireEvent.click(screen.getByRole('button', { name: '重试加载地图' }));
    const pendingButton = screen.getByRole('button', { name: '重试加载地图' }) as HTMLButtonElement;
    expect(pendingButton.disabled).toBe(true);
    fireEvent.click(pendingButton);
    expect(loadAMap).toHaveBeenCalledTimes(2);
    await act(async () => retry.resolve(SDK));
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
    expect(maps).toHaveLength(2);
    expect(maps[1].destroyed).toBe(false);
    expect(maps[1].overlays).toHaveLength(1);
    expect(fetch).not.toHaveBeenCalled();
  });

  it('handles null SDK rejection without throwing while displaying retry', async () => {
    loadAMap.mockRejectedValueOnce(null);
    render(<FullscreenMap layers={layers} bbox={bbox} onClose={() => {}} />);
    expect((await screen.findByRole('alert')).textContent).toContain('地图加载失败');
    expect(screen.getByRole('button', { name: '重试加载地图' })).toBeTruthy();
  });

  it('does not create a map when a closed fullscreen load later resolves', async () => {
    const initial = deferred<typeof SDK>();
    loadAMap.mockReturnValueOnce(initial.promise);
    const view = render(<FullscreenMap layers={layers} bbox={bbox} onClose={() => {}} />);
    view.unmount();
    await act(async () => initial.resolve(SDK));
    expect(maps).toHaveLength(0);
    expect(document.body.style.overflow).toBe('');
  });
});
