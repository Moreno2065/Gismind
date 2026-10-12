import { useId, useState } from 'react';
import type { UploadDeclaration } from '@/api/client';
import type { ArtifactSummary } from '@/types/runtime';

export interface PendingUpload {
  file: File;
  artifactId?: string;
  artifact?: ArtifactSummary;
  status: 'uploading' | 'ready' | 'error';
  error?: string;
  declaration?: UploadDeclaration;
}

interface UploadTrayProps {
  uploads: PendingUpload[];
  disabled: boolean;
  onRetry: (file: File, declaration?: UploadDeclaration) => void;
  onRemove: (file: File) => void;
}

const formats: Record<string, string> = { shapefile_zip: 'SHP ZIP', geojson: 'GeoJSON', geopackage: 'GeoPackage', gpkg: 'GeoPackage', geotiff: 'GeoTIFF', csv: 'CSV', parquet: 'Parquet', geoparquet: 'GeoParquet' };

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function crsLabel(value: unknown): string | null {
  const crs = record(value);
  if (crs.kind === 'EPSG' && typeof crs.code === 'number') return 'EPSG:' + crs.code;
  if (typeof crs.kind === 'string') return crs.kind;
  return null;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

export function UploadTray({ uploads, ...actions }: UploadTrayProps) {
  if (!uploads.length) return null;
  return <section aria-label="待发送的数据" className="mb-3 grid gap-2 sm:grid-cols-2">
    {uploads.map((upload, index) => <UploadCard key={upload.file.name + ':' + upload.file.lastModified + ':' + index} upload={upload} {...actions} />)}
  </section>;
}

function UploadCard({ upload, disabled, onRetry, onRemove }: Omit<UploadTrayProps, 'uploads'> & { upload: PendingUpload }) {
  const [declarationText, setDeclarationText] = useState('');
  const [validationError, setValidationError] = useState('');
  const helpId = useId();
  const inspection = record(upload.artifact?.schema?.inspection);
  const fields = record(inspection.fields ?? upload.artifact?.schema?.fields);
  const fieldEntries = Object.entries(fields);
  const physicalCrs = crsLabel(inspection.crs ?? upload.artifact?.crs);
  const fileSchema = record(upload.artifact?.schema?.file);
  const declaredCrs = crsLabel(upload.declaration?.declared_crs ?? record(fileSchema.declared_crs).value);
  const missingCrs = inspection.crs_status === 'missing' || /CRS_REQUIRED|MISSING_CRS|missing.{0,20}crs|crs.{0,20}missing|缺少坐标系|坐标系缺失/i.test(upload.error ?? '');
  const canDeclare = missingCrs && !physicalCrs && (!declaredCrs || upload.status === 'error') && upload.status !== 'uploading';
  const count = typeof inspection.feature_count === 'number' ? inspection.feature_count + ' 要素'
    : typeof inspection.row_count === 'number' ? inspection.row_count + ' 行'
    : typeof inspection.bands === 'number' ? inspection.bands + ' 波段' : null;
  const format = typeof inspection.format === 'string' ? formats[inspection.format] ?? inspection.format.toUpperCase() : upload.artifact?.type ?? 'FILE';
  const status = upload.status === 'ready' ? '就绪' : upload.status === 'error' ? '失败' : '上传中';
  const submitDeclaration = () => {
    if (disabled) return;
    const text = declarationText.trim().toUpperCase();
    const epsg = /^EPSG\s*:\s*([1-9]\d*)$/.exec(text);
    const code = epsg ? Number(epsg[1]) : null;
    const crs = text === 'GCJ02' ? { kind: 'GCJ02' } : code !== null && Number.isSafeInteger(code) ? { kind: 'EPSG', code } : null;
    if (!crs) { setValidationError('请输入明确的 EPSG 编号（如 EPSG:4326）或 GCJ02。'); return; }
    setValidationError('');
    onRetry(upload.file, { ...upload.declaration, declared_crs: crs });
  };

  return <article className={'group relative min-w-0 overflow-hidden rounded-xl border bg-ink-900/80 transition-colors ' + (upload.status === 'error' ? 'border-signal-error/40' : 'border-ink-600 hover:border-amber/40')}>
    <div className={'absolute inset-y-0 left-0 w-[2px] ' + (upload.status === 'error' ? 'bg-signal-error' : upload.status === 'ready' ? 'bg-amber' : 'bg-ink-500')} />
    <div className="px-3 py-3 pl-4">
      <div className="flex items-start gap-2.5">
        <div aria-hidden="true" className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-ink-600 bg-ink-800 text-amber">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.2" className="h-5 w-5"><path d="m12 3 9 5-9 5-9-5 9-5Z" /><path d="m3 12 9 5 9-5M3 16l9 5 9-5" /></svg>
        </div>
        <div className="min-w-0 flex-1">
          <p className="break-all text-xs font-medium leading-5 text-ink-100">{upload.file.name + ' · ' + status}</p>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[10px] text-ink-400"><span className="text-amber">{format}</span><span aria-hidden="true">/</span><span>{formatBytes(upload.artifact?.byte_size ?? upload.artifact?.size_bytes ?? upload.file.size)}</span>{count && <><span aria-hidden="true">/</span><span>{count}</span></>}</div>
        </div>
        <button type="button" disabled={disabled} onClick={() => onRemove(upload.file)} aria-label={'移除 ' + upload.file.name} title="从本次消息移除附件" className="flex h-6 w-6 shrink-0 items-center justify-center rounded text-base text-ink-400 hover:bg-ink-700 hover:text-ink-100 disabled:opacity-40">×</button>
      </div>

      {upload.status === 'uploading' && <div role="status" className="mt-3 flex items-center gap-2 text-[11px] text-ink-400"><span className="h-1.5 w-1.5 rounded-full bg-amber motion-safe:animate-pulse" />正在上传并检查数据…</div>}
      {upload.artifact && <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 border-t border-ink-700 pt-2 font-mono text-[10px] text-ink-300"><span><span className="mr-1.5 text-ink-500">CRS</span>{physicalCrs ?? (inspection.crs_status === 'not_applicable' ? '不适用' : '文件未记录')}</span>{declaredCrs && <span className="text-amber">用户声明 {declaredCrs}</span>}{typeof inspection.width === 'number' && typeof inspection.height === 'number' && <span>{inspection.width + ' × ' + inspection.height + ' px'}</span>}</div>}
      {fieldEntries.length > 0 && <details className="mt-2 text-[11px]"><summary className="cursor-pointer text-ink-400 hover:text-amber">{fieldEntries.length} 个字段</summary><div className="mt-2 max-h-32 overflow-auto rounded-md border border-ink-700 bg-ink-950/40 px-2 py-1">{fieldEntries.map(([name, type]) => <div key={name} className="flex justify-between gap-3 border-b border-ink-700/60 py-1 last:border-0"><span className="min-w-0 break-all font-mono text-ink-200">{name}</span><span className="shrink-0 font-mono text-ink-400">{typeof type === 'string' ? type : JSON.stringify(type)}</span></div>)}</div></details>}
      {upload.error && <p className="mt-2 break-words text-[11px] leading-relaxed text-signal-error">{upload.error}</p>}
      {canDeclare && <div className="mt-3 border-t border-amber/20 pt-3">
        <label className="block text-[11px] font-medium text-amber">补充原始坐标系<input aria-label={'坐标系声明 ' + upload.file.name} aria-describedby={helpId} value={declarationText} onChange={(event) => { setDeclarationText(event.target.value); setValidationError(''); }} disabled={disabled} placeholder="EPSG:4326 或 GCJ02" className="mt-1.5 block w-full rounded-md border border-ink-600 bg-ink-950 px-2.5 py-2 font-mono text-xs text-ink-100 outline-none placeholder:text-ink-500 focus:border-amber disabled:opacity-50" /></label>
        <p id={helpId} className="mt-1.5 text-[10px] leading-relaxed text-ink-400">请依据数据来源填写。声明仅说明原始坐标系，不会转换坐标。</p>
        {validationError && <p role="alert" className="mt-1.5 text-[11px] text-signal-error">{validationError}</p>}
        <button type="button" disabled={disabled} onClick={submitDeclaration} aria-label={'声明坐标系并重传 ' + upload.file.name} className="mt-2 rounded-md border border-amber/40 bg-amber/10 px-2.5 py-1.5 text-[11px] font-medium text-amber hover:bg-amber/20 disabled:opacity-40">确认声明并重传 <span aria-hidden="true">↗</span></button>
      </div>}
      {upload.status === 'error' && !canDeclare && <button type="button" disabled={disabled} onClick={() => onRetry(upload.file, upload.declaration)} aria-label={'重试上传 ' + upload.file.name} className="mt-2 rounded-md border border-ink-600 px-2.5 py-1 text-[11px] text-ink-200 hover:border-amber hover:text-amber disabled:opacity-40">重试</button>}
    </div>
  </article>;
}
