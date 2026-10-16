import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LazyMapView } from '@/components/LazyMapView';
import type { MapLayer } from '@/types/message';

let visibility: (entries: Array<{ isIntersecting: boolean }>) => void;
let rejectAttachment = false;
const maps: SDKMap[] = [];

class SDKMap {
  overlays: SDKOverlay[] = [];
  constructor() { maps.push(this); }
  getAllOverlays(type: string) { return this.overlays.filter(overlay => overlay.type === type); }
  setBounds() {}
  destroy() { this.overlays.forEach(overlay => { overlay.map = null; }); this.overlays = []; }
}
class SDKOverlay {
  map: SDKMap | null = null;
  constructor(public options: { position?: number[]; path?: unknown }, public type: string) {}
  setMap(map: SDKMap | null) {
    if (this.map) this.map.overlays = this.map.overlays.filter(item => item !== this);
    if (rejectAttachment && map) return;
    this.map = map;
    if (map) map.overlays.push(this);
  }
  getMap() { return this.map; }
  getPosition() { return this.options.position; }
  getPath() { return this.options.path; }
  on() {}
}
vi.mock('@/lib/amap', () => ({ loadAMap: async () => ({
  Map: SDKMap,
  Marker: class extends SDKOverlay { constructor(options: SDKOverlay['options']) { super(options, 'marker'); } },
  Polyline: class extends SDKOverlay { constructor(options: SDKOverlay['options']) { super(options, 'polyline'); } },
  Polygon: class extends SDKOverlay { constructor(options: SDKOverlay['options']) { super(options, 'polygon'); } },
  Pixel: class {}, Bounds: class {},
}) }));

const layers: MapLayer[] = [{ type: 'FeatureCollection', features: [
  { type: 'Feature', properties: { id: 'A' }, geometry: { type: 'Point', coordinates: [118.8, 32.05] } },
  { type: 'Feature', geometry: { type: 'MultiPolygon', coordinates: [
    [[[118.7, 32], [118.9, 32], [118.9, 32.1], [118.7, 32]]],
    [[[119, 32], [119.1, 32], [119.1, 32.2], [119, 32]]],
  ] } },
] }];

beforeEach(() => {
  rejectAttachment = false; maps.length = 0;
  vi.stubGlobal('IntersectionObserver', class {
    constructor(callback: typeof visibility) { visibility = callback; }
    observe() {} disconnect() {}
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function evidence(container: HTMLElement) {
  const raw = container.querySelector('[data-map-render-evidence]')?.getAttribute('data-map-render-evidence');
  expect(raw).toBeTruthy();
  return JSON.parse(raw!);
}

describe('actual map overlay evidence', () => {
  it('reads attached SDK geometry, including multipart children, after real renderer calls', async () => {
    const view = render(<LazyMapView layers={layers} bbox={[118.7, 32, 119.1, 32.2]} sourceArtifactId="final-map" />);
    act(() => visibility([{ isIntersecting: true }]));
    await screen.findByRole('button', { name: '全屏展开地图' });
    expect(evidence(view.container)).toMatchObject({ sourceArtifactId: 'final-map', observation: 'sdk-overlays',
      overlayCount: 3, geometryTypes: { Point: 1, LineString: 0, Polygon: 2 }, bounds: [118.7, 32, 119.1, 32.2] });
  });

  it('does not report requested geometry as drawn when the SDK attaches no overlays', async () => {
    rejectAttachment = true;
    const view = render(<LazyMapView layers={layers} bbox={[118.7, 32, 119.1, 32.2]} sourceArtifactId="empty-map" />);
    act(() => visibility([{ isIntersecting: true }]));
    await screen.findByRole('button', { name: '全屏展开地图' });
    expect(evidence(view.container)).toMatchObject({ overlayCount: 0, bounds: null });
  });

  it('clears stale evidence when the map sleeps and refreshes its source when geometry changes', async () => {
    const props = { layers, bbox: [118.7, 32, 119.1, 32.2] as [number, number, number, number] };
    const view = render(<LazyMapView {...props} sourceArtifactId="old-map" />);
    act(() => visibility([{ isIntersecting: true }]));
    await screen.findByRole('button', { name: '全屏展开地图' });
    view.rerender(<LazyMapView {...props} sourceArtifactId="new-map" />);
    await waitFor(() => expect(evidence(view.container).sourceArtifactId).toBe('new-map'));
    act(() => visibility([{ isIntersecting: false }]));
    await waitFor(() => expect(view.container.querySelector('[data-map-render-evidence]')).toBeNull());
  });
});
