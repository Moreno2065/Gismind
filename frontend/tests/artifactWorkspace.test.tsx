import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ArtifactWorkspace } from '@/components/ArtifactWorkspace';
import type { ArtifactSummary } from '@/types/runtime';

const wgs84 = { kind: 'EPSG', code: 4326 };
const metadata = new Map<string, ArtifactSummary>();
const contents = new Map<string, unknown>();
const rasterPreviews = new Map<string, unknown>();
const requests: Array<{ url: string; signal?: AbortSignal | null }> = [];
let respond: ((url: string, init?: RequestInit) => Promise<Response | undefined>) | undefined;

function artifact(id: string, type = 'Vector', extra: Partial<ArtifactSummary> = {}): ArtifactSummary {
  return { artifact_id: id, type, crs: wgs84, byte_size: 2048, media_type: 'application/geo+json', validation_status: 'valid', producer_task_id: 'task_buffer_01', schema: { fields: { name: 'string', population: 'integer' } }, ...extra };
}
function geojson(count = 1) {
  return { type: 'FeatureCollection', features: Array.from({ length: count }, (_, index) => ({ type: 'Feature', properties: { name: `district-${index + 1}`, population: index * 100 }, geometry: { type: 'Point', coordinates: [118.8, 32.05] } })) };
}
function json(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } }); }
function install(item: ArtifactSummary, content?: unknown) { metadata.set(item.artifact_id, item); if (content !== undefined) contents.set(item.artifact_id, content); return item; }

beforeEach(() => {
  metadata.clear(); contents.clear(); rasterPreviews.clear(); requests.length = 0; respond = undefined;
  vi.stubGlobal('IntersectionObserver', class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); requests.push({ url, signal: init?.signal });
    const override = await respond?.(url, init);
    if (override) return override;
    const match = url.match(/^\/api\/artifacts\/([^/]+)(?:\/(content|raster-preview))?$/);
    if (!match) throw new Error(`Unexpected request: ${url}`);
    const id = decodeURIComponent(match[1]);
    if (match[2] === 'raster-preview') return json(rasterPreviews.get(id) ?? { error: { code: 'RASTER_PREVIEW_UNAVAILABLE', message: '栅格预览不可用' } }, rasterPreviews.has(id) ? 200 : 422);
    return json(match[2] === 'content' ? contents.get(id) : metadata.get(id));
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('ArtifactWorkspace', () => {
  it('downloads a JSON Table with a usable filename', async () => {
    const item = install(artifact('zone-stats', 'Table', { media_type: 'application/json' }), [{ zone: 'L', mean: 3 }]);
    render(<ArtifactWorkspace artifacts={[item]} onUse={() => {}} />);
    await screen.findByRole('table', { name: '成果属性表' });
    expect(screen.getByRole('link', { name: '下载原始成果' }).getAttribute('download')).toBe('zone-stats.json');
  });

  it('links the producer only when that task is present in the displayed run', async () => {
    const item = install(artifact('lineage', 'File', { media_type: 'application/octet-stream' }));
    const task = { task_id: 'task_buffer_01', task_key: '生成缓冲区', status: 'succeeded', attempts: 1 };
    const view = render(<ArtifactWorkspace artifacts={[item]} tasks={[task]} onUse={() => {}} />);
    expect(await screen.findByRole('link', { name: '生成缓冲区' })).toBeTruthy();
    expect(screen.getByRole('link', { name: '生成缓冲区' }).getAttribute('href')).toBe('#runtime-task-task_buffer_01');
    view.rerender(<ArtifactWorkspace artifacts={[item]} tasks={[{ ...task, task_id: 'different-run-task' }]} onUse={() => {}} />);
    expect(screen.queryByRole('link', { name: '生成缓冲区' })).toBeNull();
    expect(screen.getByText('task_buffer_01')).toBeTruthy();
  });

  it('selects the latest final result instead of an older upload, while preserving manual selection', async () => {
    const upload = install(artifact('old-upload'), geojson());
    const result = install(artifact('final-result', 'File', { media_type: 'application/geopackage+sqlite3' }));
    const props = { artifacts: [upload, result], onUse: () => {}, preferredArtifactId: 'final-result', resultRunId: 'run-new' };
    const view = render(<ArtifactWorkspace {...props} />);
    await waitFor(() => expect(screen.getByRole('link', { name: /下载原始成果/ }).getAttribute('href')).toBe('/api/artifacts/final-result/content'));
    fireEvent.click(screen.getByRole('button', { name: '选择成果 old-upload' }));
    await waitFor(() => expect(screen.getByRole('link', { name: /下载原始成果/ }).getAttribute('href')).toBe('/api/artifacts/old-upload/content'));
    view.rerender(<ArtifactWorkspace {...props} artifacts={[upload, { ...result, byte_size: 4096 }]} />);
    expect(screen.getByRole('link', { name: /下载原始成果/ }).getAttribute('href')).toBe('/api/artifacts/old-upload/content');
    view.rerender(<ArtifactWorkspace {...props} resultRunId="next-run" />);
    await waitFor(() => expect(screen.getByRole('link', { name: /下载原始成果/ }).getAttribute('href')).toBe('/api/artifacts/final-result/content'));
  });

  it('shows an honest empty state without fetching invented results', () => {
    render(<ArtifactWorkspace artifacts={[]} onUse={() => {}} />);
    expect(screen.getByText('成果将出现在这里')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '用于下一步' })).toBeNull();
    expect(requests).toHaveLength(0);
  });

  it('loads only the selected artifact and exposes its metadata, real download and explicit reuse action', async () => {
    const item = install(artifact('vector/one', 'Vector', { schema: { file: { filename: 'districts.geojson' }, fields: { name: 'string', population: 'integer' } } }), geojson());
    const second = install(artifact('raster-two', 'Raster', { media_type: 'image/tiff' }));
    const onUse = vi.fn();
    render(<ArtifactWorkspace artifacts={[{ artifact_id: item.artifact_id, type: 'Vector' }, second]} onUse={onUse} />);
    await screen.findByText('task_buffer_01');
    expect(screen.getByText('EPSG:4326')).toBeTruthy();
    expect(screen.getAllByText('2.0 KB').length).toBeGreaterThan(0);
    expect(screen.getByRole('link', { name: /下载原始成果/ }).getAttribute('href')).toBe('/api/artifacts/vector%2Fone/content');
    expect(requests.every(request => !request.url.includes('raster-two'))).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '用于下一步' }));
    expect(onUse).toHaveBeenCalledWith(expect.objectContaining({ artifact_id: 'vector/one', validation_status: 'valid' }));
  });

  it('paginates Vector attributes without trimming or modifying the map source', async () => {
    const source = geojson(101);
    const item = install(artifact('vector-preview'), source);
    render(<ArtifactWorkspace artifacts={[item]} onUse={() => {}} />);
    await screen.findByText(/101 features · bbox \[118\.81, 32\.05/);
    expect(screen.getByText('第 1–100 条，共 101 条')).toBeTruthy();
    const table = screen.getByRole('table', { name: '成果属性表' });
    expect(within(table).getAllByRole('row')).toHaveLength(101);
    expect(within(table).getByText('district-100')).toBeTruthy();
    expect(within(table).queryByText('district-101')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    expect(screen.getByText('第 101–101 条，共 101 条')).toBeTruthy();
    expect(within(table).getByText('district-101')).toBeTruthy();
    expect(within(table).queryByText('district-100')).toBeNull();
    expect((screen.getByRole('button', { name: '下一页' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '上一页' }));
    expect(within(table).getByText('district-1')).toBeTruthy();
    expect(source.features[0].geometry.coordinates).toEqual([118.8, 32.05]);
  });

  it('refuses to guess a Vector CRS while retaining the readable attribute table', async () => {
    const item = install(artifact('unknown-crs', 'Vector', { crs: null }), geojson());
    render(<ArtifactWorkspace artifacts={[item]} onUse={() => {}} />);
    await screen.findByText(/缺少可信 CRS/);
    expect(screen.queryByText(/features · bbox/)).toBeNull();
    expect(screen.getByRole('table', { name: '成果属性表' })).toBeTruthy();
  });

  it('uses the existing MapLayer projection and reports conflicting metadata rather than showing a misleading map', async () => {
    const item = install(artifact('map-layer', 'MapLayer', { media_type: 'application/json' }), { crs: { kind: 'GCJ02' }, bbox: [118, 31, 119, 33], layers: [geojson()] });
    render(<ArtifactWorkspace artifacts={[item]} onUse={() => {}} />);
    await screen.findByText(/CRS 不一致/);
    expect(screen.queryByText(/features · bbox/)).toBeNull();
    expect(screen.getByRole('link', { name: /下载原始成果/ })).toBeTruthy();
  });

  it('shows the real feature attributes alongside a valid MapLayer preview', async () => {
    const item = install(artifact('valid-map', 'MapLayer', { media_type: 'application/json' }), { crs: wgs84, bbox: [118, 31, 119, 33], layers: [geojson()] });
    render(<ArtifactWorkspace artifacts={[item]} onUse={() => {}} />);
    await screen.findByText(/1 features · bbox \[118\.81, 32\.05/);
    expect(within(screen.getByRole('table', { name: '成果属性表' })).getByText('district-1')).toBeTruthy();
  });

  it('shows a georeferenced raster preview and preserves the original GeoTIFF link', async () => {
    const item = install(artifact('terrain', 'Raster', { media_type: 'image/tiff' }));
    rasterPreviews.set('terrain', { artifact_id: 'terrain', crs: { kind: 'WGS84' }, bbox: [118, 32, 119, 33],
      width: 4, height: 3, png_b64: 'aW1hZ2U=', band: 1, band_count: 2, nodata: '-9999', value_range: [0, 700], masked_pixels: 3 });
    render(<ArtifactWorkspace artifacts={[item]} onUse={() => {}} disabled />);
    const map = await screen.findByRole('figure', { name: 'WGS84 栅格地图' });
    expect(within(map).getByRole('img', { name: '栅格波段 1 的空间预览' }).getAttribute('src')).toBe('data:image/png;base64,aW1hZ2U=');
    expect(within(map).getByText('118°E')).toBeTruthy();
    expect(within(map).getByText('33°N')).toBeTruthy();
    expect(within(map).getByText('波段 1 / 2')).toBeTruthy();
    expect(within(map).getByText('0 – 700')).toBeTruthy();
    expect(within(map).getByText('NoData -9999')).toBeTruthy();
    expect(requests.some(request => request.url.endsWith('/content'))).toBe(false);
    expect(screen.getByRole('link', { name: /下载原始成果/ }).getAttribute('href')).toBe('/api/artifacts/terrain/content');
    expect(screen.getByRole('link', { name: /下载原始成果/ }).getAttribute('download')).toBe('terrain.tif');
    expect((screen.getByRole('button', { name: '用于下一步' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('retries a busy preview for an inspected GeoTIFF upload while leaving the source usable', async () => {
    const item = install(artifact('dem-upload', 'File', { media_type: 'application/octet-stream',
      schema: { file: { filename: 'dem.tiff' }, inspection: { format: 'geotiff' } } }));
    let first = true;
    respond = async url => {
      if (!url.endsWith('/raster-preview') || !first) return undefined;
      first = false;
      return json({ error: { code: 'RASTER_PREVIEW_BUSY', message: '请稍后重试' } }, 503);
    };
    rasterPreviews.set(item.artifact_id, { artifact_id: item.artifact_id, crs: { kind: 'WGS84' }, bbox: [118, 32, 119, 33],
      width: 4, height: 3, png_b64: 'aW1hZ2U=', band: 1, band_count: 1, nodata: null, value_range: null, masked_pixels: 12 });
    render(<ArtifactWorkspace artifacts={[item]} onUse={() => {}} />);
    const retry = await screen.findByRole('button', { name: '重试预览' });
    expect((screen.getByRole('button', { name: '用于下一步' }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByRole('link', { name: '下载原始成果' }).getAttribute('download')).toBe('dem.tiff');
    fireEvent.click(retry);
    const map = await screen.findByRole('figure', { name: 'WGS84 栅格地图' });
    expect(within(map).getByText('没有有效预览像元')).toBeTruthy();
    expect(within(map).getByText('棋盘格表示无效或覆盖范围外')).toBeTruthy();
  });

  it('lets the user retry a failed content read without losing access to the artifact', async () => {
    const item = install(artifact('retry-vector'), geojson());
    let failed = false;
    respond = async url => {
      if (url.endsWith('/content') && !failed) { failed = true; return json({ error: { code: 'UNAVAILABLE', message: '成果暂时无法读取' } }, 503); }
      return undefined;
    };
    render(<ArtifactWorkspace artifacts={[item]} onUse={() => {}} />);
    await screen.findByText(/成果暂时无法读取/);
    expect(screen.getByRole('link', { name: /下载原始成果/ })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '重试加载' }));
    await screen.findByText('district-1');
    expect(screen.queryByText(/成果暂时无法读取/)).toBeNull();
  });

  it('aborts an obsolete content request when selection changes and ignores a late response', async () => {
    const first = install(artifact('slow-vector'), geojson());
    const second = install(artifact('new-raster', 'Raster', { media_type: 'image/tiff' }));
    let finish!: (response: Response) => void;
    respond = async url => url.endsWith('/slow-vector/content') ? new Promise(resolve => { finish = resolve; }) : undefined;
    render(<ArtifactWorkspace artifacts={[first, second]} onUse={() => {}} />);
    await waitFor(() => expect(requests.some(request => request.url.endsWith('/slow-vector/content'))).toBe(true));
    fireEvent.click(screen.getByRole('button', { name: /选择成果 new-raster/ }));
    expect((await screen.findByRole('alert')).textContent).toContain('栅格预览不可用');
    expect(requests.find(request => request.url.endsWith('/slow-vector/content'))?.signal?.aborted).toBe(true);
    await act(async () => { finish(json(geojson())); });
    expect(screen.queryByRole('table', { name: '成果属性表' })).toBeNull();
    expect(screen.getByRole('link', { name: /下载原始成果/ }).getAttribute('href')).toBe('/api/artifacts/new-raster/content');
  });

  it('cancels metadata loading when the parent replaces the session key', async () => {
    const old = install(artifact('old-session'));
    let finish!: (response: Response) => void;
    respond = async url => url.endsWith('/old-session') ? new Promise(resolve => { finish = resolve; }) : undefined;
    const view = render(<ArtifactWorkspace key="session-a" artifacts={[old]} onUse={() => {}} />);
    await waitFor(() => expect(requests).toHaveLength(1));
    view.rerender(<ArtifactWorkspace key="session-b" artifacts={[]} onUse={() => {}} />);
    expect(requests[0].signal?.aborted).toBe(true);
    await act(async () => { finish(json(old)); });
    expect(screen.getByText('成果将出现在这里')).toBeTruthy();
    expect(screen.queryByText('task_buffer_01')).toBeNull();
  });
  it('does not offer reuse of unverified summary metadata after its metadata request fails', async () => {
    const item = install(artifact('metadata-failed'));
    respond = async () => json({ error: { code: 'ARTIFACT_NOT_FOUND', message: '成果已不可用' } }, 404);
    const onUse = vi.fn();
    render(<ArtifactWorkspace artifacts={[item]} onUse={onUse} />);
    await screen.findByText(/成果已不可用/);
    expect((screen.getByRole('button', { name: '用于下一步' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '用于下一步' }));
    expect(onUse).not.toHaveBeenCalled();
  });
});
