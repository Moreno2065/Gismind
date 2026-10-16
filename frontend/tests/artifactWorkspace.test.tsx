import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ArtifactWorkspace } from '@/components/ArtifactWorkspace';
import type { ArtifactSummary } from '@/types/runtime';

const wgs84 = { kind: 'EPSG', code: 4326 };
const metadata = new Map<string, ArtifactSummary>();
const contents = new Map<string, unknown>();
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
  metadata.clear(); contents.clear(); requests.length = 0; respond = undefined;
  vi.stubGlobal('IntersectionObserver', class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); requests.push({ url, signal: init?.signal });
    const override = await respond?.(url, init);
    if (override) return override;
    const match = url.match(/^\/api\/artifacts\/([^/]+)(\/content)?$/);
    if (!match) throw new Error(`Unexpected request: ${url}`);
    const id = decodeURIComponent(match[1]);
    return json(match[2] ? contents.get(id) : metadata.get(id));
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('ArtifactWorkspace', () => {
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

  it('creates a safe projected Vector preview and limits the attribute table to the first 100 features', async () => {
    const source = geojson(101);
    const item = install(artifact('vector-preview'), source);
    render(<ArtifactWorkspace artifacts={[item]} onUse={() => {}} />);
    await screen.findByText(/101 features · bbox \[118\.81, 32\.05/);
    expect(screen.getByText('仅显示前 100 条，共 101 条')).toBeTruthy();
    const table = screen.getByRole('table', { name: '成果属性表' });
    expect(within(table).getAllByRole('row')).toHaveLength(101);
    expect(within(table).getByText('district-100')).toBeTruthy();
    expect(within(table).queryByText('district-101')).toBeNull();
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

  it('does not parse Raster bytes as JSON and clearly states the browser preview boundary', async () => {
    const item = install(artifact('terrain', 'Raster', { media_type: 'image/tiff' }));
    render(<ArtifactWorkspace artifacts={[item]} onUse={() => {}} disabled />);
    await screen.findByText('可下载 GeoTIFF，尚无浏览器栅格预览');
    expect(requests.some(request => request.url.endsWith('/content'))).toBe(false);
    expect((screen.getByRole('button', { name: '用于下一步' }) as HTMLButtonElement).disabled).toBe(true);
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
    await screen.findByText('可下载 GeoTIFF，尚无浏览器栅格预览');
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
