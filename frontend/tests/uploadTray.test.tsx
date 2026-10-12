import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { uploadRuntimeArtifact } from '@/api/client';
import { UploadTray } from '@/components/UploadTray';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('upload declaration transport', () => {
  it('sends user CRS and bounds as JSON multipart fields with the original signal', async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ artifact_id: 'art_uploaded', type: 'File' })));
    vi.stubGlobal('fetch', fetcher);
    const file = new File(['data'], 'roads.zip');
    const signal = new AbortController().signal;
    await uploadRuntimeArtifact(file, signal, { declared_crs: { kind: 'EPSG', code: 32650 }, declared_bbox: [1, 2, 3, 4] });
    const init = fetcher.mock.calls[0][1] as RequestInit;
    const body = init.body as FormData;
    expect(body.get('file')).toBe(file);
    expect(body.get('declared_crs')).toBe('{"kind":"EPSG","code":32650}');
    expect(body.get('declared_bbox')).toBe('[1,2,3,4]');
    expect(init.signal).toBe(signal);
  });

  it('does not invent declaration metadata for the existing two-argument call', async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ artifact_id: 'art_uploaded', type: 'File' })));
    vi.stubGlobal('fetch', fetcher);
    await uploadRuntimeArtifact(new File(['data'], 'roads.zip'), new AbortController().signal);
    const body = fetcher.mock.calls[0][1]?.body as FormData;
    expect(body.has('declared_crs')).toBe(false);
    expect(body.has('declared_bbox')).toBe(false);
  });
});

describe('upload data cards', () => {
  const file = new File(['data'], 'roads.zip');
  const artifact = { artifact_id: 'art_roads', type: 'File', byte_size: 2048, schema: { inspection: { format: 'shapefile_zip', crs: { kind: 'EPSG', code: 4326 }, crs_status: 'present', feature_count: 28, fields: { name: 'String', length: 'Float64' } } } };

  it('shows inspected format, CRS, counts, fields and physical bytes', () => {
    render(<UploadTray uploads={[{ file, status: 'ready', artifact }]} disabled={false} onRetry={vi.fn()} onRemove={vi.fn()} />);
    expect(screen.getByText('roads.zip · 就绪')).toBeTruthy();
    expect(screen.getByText('SHP ZIP')).toBeTruthy();
    expect(screen.getByText('EPSG:4326')).toBeTruthy();
    expect(screen.getByText('28 要素')).toBeTruthy();
    expect(screen.getByText('2.0 KB')).toBeTruthy();
    fireEvent.click(screen.getByText('2 个字段'));
    expect(screen.getByText('name')).toBeTruthy();
    expect(screen.getByText('Float64')).toBeTruthy();
  });

  it('requires an explicit CRS for missing-CRS retry and passes the declaration', () => {
    const onRetry = vi.fn();
    render(<UploadTray uploads={[{ file, status: 'error', error: 'CRS_REQUIRED: 文件缺少坐标系' }]} disabled={false} onRetry={onRetry} onRemove={vi.fn()} />);
    const field = screen.getByLabelText('坐标系声明 roads.zip') as HTMLInputElement;
    expect(field.value).toBe('');
    fireEvent.click(screen.getByRole('button', { name: '声明坐标系并重传 roads.zip' }));
    expect(onRetry).not.toHaveBeenCalled();
    fireEvent.change(field, { target: { value: 'EPSG:32650' } });
    fireEvent.click(screen.getByRole('button', { name: '声明坐标系并重传 roads.zip' }));
    expect(onRetry).toHaveBeenCalledWith(file, { declared_crs: { kind: 'EPSG', code: 32650 } });
  });

  it('reads raster band counts and dimensions from the physical inspection schema', () => {
    render(<UploadTray uploads={[{ file, status: 'ready', artifact: { ...artifact, schema: { inspection: { format: 'geotiff', bands: 3, width: 128, height: 64, crs: { kind: 'EPSG', code: 3857 } } } } }]} disabled={false} onRetry={vi.fn()} onRemove={vi.fn()} />);
    expect(screen.getByText('3 波段')).toBeTruthy();
    expect(screen.getByText('128 × 64 px')).toBeTruthy();
  });

  it('lets a successfully inspected upload with missing CRS declare GCJ02 without implying a conversion', () => {
    const onRetry = vi.fn();
    render(<UploadTray uploads={[{ file, status: 'ready', artifact: { ...artifact, schema: { inspection: { format: 'geojson', crs: null, crs_status: 'missing' } } } }]} disabled={false} onRetry={onRetry} onRemove={vi.fn()} />);
    fireEvent.change(screen.getByLabelText('坐标系声明 roads.zip'), { target: { value: 'GCJ02' } });
    fireEvent.click(screen.getByRole('button', { name: '声明坐标系并重传 roads.zip' }));
    expect(onRetry).toHaveBeenCalledWith(file, { declared_crs: { kind: 'GCJ02' } });
    expect(screen.getByText(/声明仅说明原始坐标系/)).toBeTruthy();
  });

  it('rejects malformed declarations and keeps removal scoped to the attachment', () => {
    const onRetry = vi.fn(); const onRemove = vi.fn();
    render(<UploadTray uploads={[{ file, status: 'error', error: 'CRS_REQUIRED' }]} disabled={false} onRetry={onRetry} onRemove={onRemove} />);
    fireEvent.change(screen.getByLabelText('坐标系声明 roads.zip'), { target: { value: 'probably WGS84' } });
    fireEvent.click(screen.getByRole('button', { name: '声明坐标系并重传 roads.zip' }));
    expect(onRetry).not.toHaveBeenCalled();
    expect(screen.getByRole('alert').textContent).toMatch(/EPSG/);
    fireEvent.click(screen.getByRole('button', { name: '移除 roads.zip' }));
    expect(onRemove).toHaveBeenCalledWith(file);
  });

  it('keeps ordinary retry compatible and disables mutations while submitting', () => {
    const onRetry = vi.fn(); const onRemove = vi.fn();
    const view = render(<UploadTray uploads={[{ file, status: 'error', error: '网络不可用' }]} disabled={false} onRetry={onRetry} onRemove={onRemove} />);
    fireEvent.click(screen.getByRole('button', { name: '重试上传 roads.zip' }));
    expect(onRetry).toHaveBeenCalledWith(file, undefined);
    view.rerender(<UploadTray uploads={[{ file, status: 'error', error: '网络不可用' }]} disabled onRetry={onRetry} onRemove={onRemove} />);
    fireEvent.click(screen.getByRole('button', { name: '移除 roads.zip' }));
    expect(onRemove).not.toHaveBeenCalled();
    expect((screen.getByRole('button', { name: '重试上传 roads.zip' }) as HTMLButtonElement).disabled).toBe(true);
  });
});
