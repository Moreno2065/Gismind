import { useEffect, useMemo, useState } from 'react';
import { getRuntimeArtifact, getRuntimeArtifactContent, runtimeArtifactContentUrl } from '@/api/client';
import { projectRuntimeMapLayer, type RuntimeMapDisplay } from '@/lib/runtimeMapProjection';
import type { ArtifactSummary } from '@/types/runtime';
import { LazyMapView } from './LazyMapView';

interface ArtifactWorkspaceProps {
  artifacts: ArtifactSummary[];
  onUse: (artifact: ArtifactSummary) => void;
  disabled?: boolean;
}

type JsonObject = Record<string, unknown>;
interface LoadedArtifact {
  id: string;
  artifact?: ArtifactSummary;
  content?: unknown;
  loading: boolean;
  error?: string;
}
interface Preview {
  map?: RuntimeMapDisplay;
  rows: JsonObject[];
  total: number;
  note?: string;
}

const PREVIEW_LIMIT = 100;
const JSON_PREVIEW_TYPES = new Set(['Vector', 'MapLayer', 'Table', 'POISet', 'Coordinate']);

function object(value: unknown): JsonObject | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : undefined;
}

function text(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'string') return value;
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

function artifactName(artifact: ArtifactSummary): string {
  const schema = artifact.schema;
  const name = object(schema?.file)?.filename ?? schema?.filename ?? schema?.name;
  return typeof name === 'string' && name.trim() ? name : artifact.artifact_id;
}

function formatSize(artifact: ArtifactSummary): string {
  const bytes = artifact.byte_size ?? artifact.size_bytes;
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return '未提供';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatCRS(artifact: ArtifactSummary): string {
  const crs = artifact.crs;
  if (!crs) return '未声明';
  if (crs.kind === 'EPSG') return `EPSG:${text(crs.code)}`;
  if (crs.kind === 'WGS84') return `WGS84${crs.axis_order ? ` · ${text(crs.axis_order)}` : ''}`;
  return text(crs.kind ?? crs);
}

function schemaFields(artifact: ArtifactSummary): Array<[string, string]> {
  const fields = artifact.schema?.fields ?? artifact.schema?.columns;
  if (Array.isArray(fields)) return fields.flatMap(field => {
    if (typeof field === 'string') return [[field, ''] as [string, string]];
    const entry = object(field);
    return entry && typeof entry.name === 'string' ? [[entry.name, text(entry.type ?? entry.dtype ?? '')] as [string, string]] : [];
  });
  return Object.entries(object(fields) ?? {}).map(([name, value]) => [name, text(object(value)?.type ?? value)]);
}

function canReadJson(artifact: ArtifactSummary): boolean {
  if (!JSON_PREVIEW_TYPES.has(artifact.type)) return false;
  const mediaType = artifact.media_type?.split(';')[0].trim().toLowerCase();
  return !mediaType || mediaType === 'application/json' || mediaType.endsWith('+json');
}

/** Only geometry coordinates supply the source bbox; CRS validation stays in the shared projector. */
function vectorEnvelope(content: JsonObject, crs: unknown): JsonObject {
  const bounds = [Infinity, Infinity, -Infinity, -Infinity];
  const collect = (coordinates: unknown): void => {
    if (!Array.isArray(coordinates)) return;
    if (coordinates.length >= 2 && typeof coordinates[0] === 'number' && typeof coordinates[1] === 'number') {
      const [x, y] = coordinates;
      if (Number.isFinite(x) && Number.isFinite(y)) {
        bounds[0] = Math.min(bounds[0], x); bounds[1] = Math.min(bounds[1], y);
        bounds[2] = Math.max(bounds[2], x); bounds[3] = Math.max(bounds[3], y);
      }
    } else coordinates.forEach(collect);
  };
  for (const feature of content.features as unknown[]) collect(object(object(feature)?.geometry)?.coordinates);
  if (!bounds.every(Number.isFinite)) throw new Error('没有可显示的几何坐标，可在属性表中检查数据。');
  return { crs, bbox: bounds, layers: [content] };
}

function previewFor(artifact: ArtifactSummary, content: unknown): Preview {
  const result: Preview = { rows: [], total: 0 };
  if (artifact.type === 'Raster') return { ...result, note: '可下载 GeoTIFF，尚无浏览器栅格预览' };
  if (content === undefined) return result;
  const value = object(content);
  const features = value?.type === 'FeatureCollection' && Array.isArray(value.features) ? value.features : undefined;
  if (features) {
    result.total = features.length;
    result.rows = features.slice(0, PREVIEW_LIMIT).map(feature => object(object(feature)?.properties) ?? {});
  } else if (artifact.type === 'Table') {
    const rows = Array.isArray(content) ? content : Array.isArray(value?.rows) ? value.rows : [];
    result.total = rows.length;
    result.rows = rows.slice(0, PREVIEW_LIMIT).map(row => object(row) ?? { value: row });
  }
  try {
    if (artifact.type === 'MapLayer') {
      result.map = projectRuntimeMapLayer(content, artifact.crs);
      for (const layer of Array.isArray(value?.layers) ? value.layers : []) {
        const collection = object(layer);
        if (collection?.type !== 'FeatureCollection' || !Array.isArray(collection.features)) continue;
        result.total += collection.features.length;
        result.rows.push(...collection.features.slice(0, PREVIEW_LIMIT - result.rows.length).map(feature => object(object(feature)?.properties) ?? {}));
      }
    } else if (artifact.type === 'Vector') {
      if (!features) result.note = '此成果不是可预览的 GeoJSON FeatureCollection，请下载查看。';
      else if (!artifact.crs) result.note = '缺少可信 CRS，暂不显示地图；可查看属性或下载原始成果。';
      else if (features.length === 0) result.note = '此矢量成果包含 0 个要素。';
      else result.map = projectRuntimeMapLayer(vectorEnvelope(value!, artifact.crs), artifact.crs);
    } else if (!result.rows.length) result.note = '此格式暂不支持浏览器预览，可下载原始成果。';
  } catch (error) {
    result.note = error instanceof Error ? error.message : '无法安全显示此成果的地图。';
  }
  return result;
}

export function ArtifactWorkspace({ artifacts, onUse, disabled = false }: ArtifactWorkspaceProps) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [loaded, setLoaded] = useState<LoadedArtifact | null>(null);
  const summary = artifacts.find(artifact => artifact.artifact_id === selectedId) ?? artifacts[0];
  const currentId = summary?.artifact_id;
  const current = loaded?.id === currentId ? loaded : null;
  const selected = useMemo(() => current?.artifact ? { ...summary, ...current.artifact } : summary, [summary, current?.artifact]);

  useEffect(() => {
    if (!currentId) return;
    const controller = new AbortController();
    setLoaded({ id: currentId, loading: true });
    void (async () => {
      try {
        const artifact = await getRuntimeArtifact(currentId, controller.signal);
        if (controller.signal.aborted) return;
        if (artifact.artifact_id !== currentId) throw new Error('成果标识与服务端响应不一致，请重试。');
        const readContent = canReadJson(artifact);
        setLoaded({ id: currentId, artifact, loading: readContent });
        if (readContent) {
          const content = await getRuntimeArtifactContent(currentId, controller.signal);
          if (!controller.signal.aborted) setLoaded({ id: currentId, artifact, content, loading: false });
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        setLoaded(previous => ({ ...previous, id: currentId, loading: false, error: error instanceof Error ? error.message : '成果加载失败，请重试。' }));
      }
    })();
    return () => controller.abort();
  }, [currentId, retry]);

  const preview = useMemo(() => selected ? previewFor(selected, current?.content) : null, [selected, current?.content]);
  const fields = selected ? schemaFields(selected) : [];
  const isLoading = Boolean(currentId && (!current || current.loading));

  if (!selected) return <section className="flex min-h-[380px] flex-1 flex-col items-center justify-center px-6 py-12 text-center" aria-label="成果工作区">
    <ContourMark />
    <p className="mt-7 font-mono text-[10px] tracking-[0.26em] text-amber/80">SPATIAL WORKSPACE</p>
    <h2 className="mt-3 font-display text-2xl text-ink-100">成果将出现在这里</h2>
    <p className="mt-3 max-w-xs text-sm leading-7 text-ink-400">上传一份数据，或在对话中开始空间分析。<br />在这里检查、预览，并接着处理你的成果。</p>
    <div className="mt-8 flex items-center gap-4 font-mono text-[10px] tracking-wider text-ink-500"><span>数据</span><span aria-hidden="true" className="h-px w-7 bg-ink-700" /><span>分析</span><span aria-hidden="true" className="h-px w-7 bg-ink-700" /><span>成果</span></div>
  </section>;

  return <section aria-label="成果工作区" className="flex min-h-0 min-w-0 flex-col text-ink-200">
    <header className="flex items-center justify-between gap-3 px-5 pb-3 pt-5 sm:px-6">
      <div className="flex items-baseline gap-3"><h2 className="font-display text-lg text-ink-100">空间成果</h2><span className="font-mono text-[10px] tracking-widest text-ink-500">COLLECTION</span></div>
      <span className="font-mono text-sm tabular-nums text-amber">{String(artifacts.length).padStart(2, '0')}</span>
    </header>
    <div className="flex gap-2 overflow-x-auto border-b border-ink-700/80 px-5 pb-4 sm:px-6" aria-label="成果列表">
      {artifacts.map((artifact, index) => {
        const active = artifact.artifact_id === currentId;
        const item = active ? selected : artifact;
        return <button key={artifact.artifact_id} type="button" aria-label={`选择成果 ${artifactName(item)}`} aria-pressed={active} onClick={() => setSelectedId(artifact.artifact_id)} className={`group flex w-48 shrink-0 items-start gap-3 border px-3 py-3 text-left transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-amber ${active ? 'border-amber/55 bg-amber/[0.06]' : 'border-ink-700 bg-ink-950/30 hover:border-ink-500 hover:bg-ink-800/50'}`}>
          <span className={`mt-0.5 font-mono text-[10px] tabular-nums ${active ? 'text-amber' : 'text-ink-500'}`}>{String(index + 1).padStart(2, '0')}</span>
          <span className="min-w-0"><span className="block truncate text-xs font-medium text-ink-200" title={artifactName(item)}>{artifactName(item)}</span><span className="mt-1.5 block font-mono text-[9px] tracking-wide text-ink-400">{item.type} <span aria-hidden="true" className="mx-1 text-ink-600">/</span> {formatSize(item)}</span></span>
        </button>;
      })}
    </div>
    <div className="min-w-0 space-y-5 px-5 py-5 sm:px-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 flex-1"><p className="mb-2 flex items-center gap-2 font-mono text-[9px] tracking-[0.16em] text-amber"><span className="inline-block h-1 w-1 bg-amber" aria-hidden="true" />{selected.type.toUpperCase()}</p><h3 className="break-all text-base font-medium text-ink-100">{artifactName(selected)}</h3><p className="mt-1 break-all font-mono text-[9px] leading-5 text-ink-500">{selected.artifact_id}</p></div>
        <button type="button" onClick={() => onUse(selected)} disabled={disabled || isLoading || !current?.artifact || !!current.error} className="inline-flex min-h-9 items-center gap-2 border border-amber/45 bg-amber/10 px-3 text-xs font-medium text-amber transition-colors hover:bg-amber/20 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-amber disabled:cursor-not-allowed disabled:opacity-40">用于下一步<svg aria-hidden="true" width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M3 8h10M8 3l5 5-5 5" /></svg></button>
      </div>

      <dl className="grid grid-cols-2 gap-x-5 gap-y-4 border-y border-ink-700/75 py-4 sm:grid-cols-3">
        <Metadata label="坐标系" value={formatCRS(selected)} />
        <Metadata label="文件大小" value={formatSize(selected)} />
        <Metadata label="验证状态" value={selected.validation_status === 'valid' ? '已验证 · valid' : selected.validation_status ?? '未提供'} />
        <Metadata label="来源任务" value={selected.producer_task_id ?? (selected.origin === 'upload' ? '用户上传' : '未提供')} wide />
        <Metadata label="媒体类型" value={selected.media_type ?? '未提供'} />
      </dl>

      {current?.error && <div role="alert" className="flex flex-wrap items-center justify-between gap-3 border border-signal-error/25 bg-signal-error/5 px-3 py-3"><p className="text-xs leading-6 text-signal-error">{current.error}</p><button type="button" onClick={() => setRetry(value => value + 1)} className="min-h-8 shrink-0 border border-ink-600 px-3 text-xs text-ink-200 hover:border-amber hover:text-amber">重试加载</button></div>}

      {isLoading && <div role="status" className="flex items-center gap-3 border border-ink-700/70 bg-ink-900/40 px-4 py-6"><span aria-hidden="true" className="h-3 w-3 animate-spin rounded-full border border-ink-600 border-t-amber motion-reduce:animate-none" /><span className="text-xs text-ink-400">正在读取成果…</span></div>}

      {!isLoading && !current?.error && <div>
        <div className="flex items-center justify-between"><h4 className="font-mono text-[10px] tracking-wider text-ink-400">空间预览</h4>{preview?.map && <span className="text-[10px] text-ink-500">地图显示副本 · GCJ02</span>}</div>
        {preview?.map && <LazyMapView key={selected.artifact_id} layers={preview.map.layers} bbox={preview.map.bbox} />}
        {preview?.note && <div className="mt-3 flex items-start gap-3 border border-ink-700/80 bg-ink-900/40 px-4 py-5"><svg aria-hidden="true" className="mt-0.5 shrink-0 text-ink-500" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.2"><rect x="3" y="3" width="18" height="18" rx="1" /><path d="M3 9h18M9 3v18M15 3v18M3 15h18" /></svg><p className="text-xs leading-6 text-ink-400">{preview.note}</p></div>}
        {!preview?.map && !preview?.note && !preview?.rows.length && <p className="mt-3 border border-ink-700/70 px-4 py-5 text-xs leading-6 text-ink-400">此格式暂不支持浏览器预览，可下载原始成果。</p>}
      </div>}

      {Boolean(fields.length) && <div><div className="mb-2.5 flex items-center gap-2"><h4 className="text-xs text-ink-400">数据字段</h4><span className="font-mono text-[10px] text-ink-500">{fields.length}</span></div><div className="flex flex-wrap gap-1.5">{fields.map(([name, type]) => <span key={name} className="max-w-full break-all border border-ink-700/80 bg-ink-900/30 px-2 py-1 font-mono text-[10px] leading-5"><span className="text-ink-300">{name}</span>{type && <span className="ml-2 text-ink-500">{type}</span>}</span>)}</div></div>}

      {preview && preview.rows.length > 0 && <AttributeTable preview={preview} />}

      <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-ink-700/75 pt-4">
        <span className="font-mono text-[9px] tracking-wide text-ink-500">原始数据与坐标系保持不变</span>
        <a href={runtimeArtifactContentUrl(selected)} download className="inline-flex min-h-9 items-center gap-2 text-xs text-ink-300 underline decoration-ink-600 underline-offset-4 transition-colors hover:text-amber focus-visible:outline focus-visible:outline-2 focus-visible:outline-amber"><svg aria-hidden="true" width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.25"><path d="M10 2v10M6 8l4 4 4-4M3 13v4h14v-4" /></svg>下载原始成果</a>
      </footer>
    </div>
  </section>;
}

function Metadata({ label, value, wide = false }: { label: string; value: string; wide?: boolean }) {
  return <div className={`min-w-0 ${wide ? 'col-span-2' : ''}`}><dt className="mb-1.5 text-[10px] text-ink-500">{label}</dt><dd className="break-all font-mono text-[11px] leading-5 text-ink-300">{value}</dd></div>;
}

function AttributeTable({ preview }: { preview: Preview }) {
  const columns = [...new Set(preview.rows.flatMap(row => Object.keys(row)))];
  return <div className="min-w-0"><div className="mb-3 flex flex-wrap items-center justify-between gap-2"><h4 className="text-xs text-ink-400">属性预览</h4><p className="font-mono text-[10px] text-ink-500">{preview.total > PREVIEW_LIMIT ? `仅显示前 ${PREVIEW_LIMIT} 条，共 ${preview.total} 条` : `${preview.total} 条记录`}</p></div><div className="max-h-72 overflow-auto border border-ink-700/80"><table aria-label="成果属性表" className="w-full border-collapse whitespace-nowrap text-left text-[11px]"><thead className="sticky top-0 bg-ink-800"><tr><th scope="col" className="border-b border-r border-ink-700 px-3 py-2.5 font-mono font-normal text-ink-500">#</th>{columns.map(column => <th scope="col" key={column} className="border-b border-ink-700 px-3 py-2.5 font-mono font-normal text-ink-300">{column}</th>)}</tr></thead><tbody>{preview.rows.map((row, index) => <tr key={index} className="border-b border-ink-700/50 last:border-0 even:bg-ink-900/40 hover:bg-amber/5"><th scope="row" className="border-r border-ink-700/60 px-3 py-2 font-mono font-normal tabular-nums text-ink-500">{index + 1}</th>{columns.map(column => <td key={column} className="max-w-56 truncate px-3 py-2 text-ink-300" title={text(row[column])}>{text(row[column])}</td>)}</tr>)}</tbody></table></div></div>;
}

/** Decorative contours describe the workspace, never an actual dataset. */
function ContourMark() {
  return <svg aria-hidden="true" viewBox="0 0 220 150" className="h-36 w-56 text-amber" fill="none">
    <path d="M10 35h200M10 75h200M10 115h200M30 15v120M70 15v120M110 15v120M150 15v120M190 15v120" stroke="currentColor" strokeOpacity=".07" />
    <g stroke="currentColor" strokeWidth=".8"><path d="M32 103c-19-20 12-48 38-48s31-41 58-33 35 28 44 50 22 30 4 43-55 5-73 13-52-5-71-25Z" strokeOpacity=".2" /><path d="M48 99c-13-15 11-31 32-33s30-32 48-28 25 21 30 37 17 24 1 31-40 2-55 10-41-2-56-17Z" strokeOpacity=".35" /><path d="M69 94c-9-9 13-20 24-23s24-22 36-15 10 19 18 30-6 15-20 15-41 12-58-7Z" strokeOpacity=".55" /><path d="M96 90c-10-5 14-13 21-17s15 6 14 13-23 10-35 4Z" strokeOpacity=".8" /></g>
    <circle cx="118" cy="82" r="3" fill="currentColor" /><path d="M118 62v-8M118 110v-8M90 82h8M138 82h8" stroke="currentColor" strokeOpacity=".5" />
    <path d="M10 25V15h10M200 15h10v10M210 125v10h-10M20 135H10v-10" stroke="currentColor" strokeOpacity=".35" />
  </svg>;
}
