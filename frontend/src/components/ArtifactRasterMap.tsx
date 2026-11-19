import { useEffect, useState } from 'react';
import { getRuntimeRasterPreview } from '@/api/client';
import type { ArtifactRasterPreview } from '@/types/runtime';

const coordinate = new Intl.NumberFormat('en-US', { maximumFractionDigits: 8, useGrouping: false });
const valueFormat = new Intl.NumberFormat('en-US', { maximumSignificantDigits: 8, useGrouping: false });

function degrees(value: number, positive: string, negative: string): string {
  return `${coordinate.format(Math.abs(value))}°${value < 0 ? negative : positive}`;
}

export function ArtifactRasterMap({ artifactId }: { artifactId: string }) {
  const [preview, setPreview] = useState<ArtifactRasterPreview>();
  const [error, setError] = useState<string>();
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setError(undefined);
    setPreview(undefined);
    void getRuntimeRasterPreview(artifactId, controller.signal).then(result => {
      if (controller.signal.aborted) return;
      if (result.artifact_id !== artifactId || result.crs.kind !== 'WGS84') throw new Error('栅格预览与当前成果不一致');
      setPreview(result);
    }).catch(reason => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '无法读取栅格预览');
    });
    return () => controller.abort();
  }, [artifactId, retry]);

  if (error) return <div role="alert" className="mt-3 flex flex-wrap items-center justify-between gap-3 border border-ink-700/80 bg-ink-900/40 px-4 py-3">
    <p className="text-xs leading-6 text-ink-300">{error}</p>
    <button type="button" onClick={() => setRetry(value => value + 1)} className="min-h-8 border border-ink-600 px-3 text-xs text-ink-200 hover:border-amber hover:text-amber focus-visible:outline focus-visible:outline-amber">重试预览</button>
  </div>;
  if (!preview) return <p role="status" className="mt-3 border border-ink-700/80 px-4 py-5 text-xs text-ink-400">正在生成栅格预览…</p>;

  const [west, south, east, north] = preview.bbox;
  const width = Math.min(512, (320 * preview.width) / preview.height);
  return <figure aria-label="WGS84 栅格地图" className="mt-3 overflow-hidden border border-ink-700/80 bg-ink-950/60">
    <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-2 px-3 pt-3 font-mono text-[10px] tabular-nums text-ink-300">
      <span>{degrees(west, 'E', 'W')}</span>
      <span aria-label="北侧纬度">{degrees(north, 'N', 'S')}</span>
      <span className="text-right">{degrees(east, 'E', 'W')}</span>
    </div>
    <div className="mx-3 my-2 flex justify-center border border-ink-700/80 bg-ink-900/60 p-2">
      <img src={`data:image/png;base64,${preview.png_b64}`} alt={`栅格波段 ${preview.band} 的空间预览`}
        className="block h-auto max-w-full border border-ink-500 [image-rendering:pixelated]"
        style={{ width, background: 'repeating-conic-gradient(#26303a 0% 25%, #16202a 0% 50%) 50% / 12px 12px' }} />
    </div>
    <div className="px-3 pb-2 text-center font-mono text-[10px] tabular-nums text-ink-300">{degrees(south, 'N', 'S')}</div>
    <figcaption className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-ink-700/80 px-3 py-3 text-[11px] leading-4 text-ink-300">
      <span className="text-amber">WGS84</span>
      <span>波段 {preview.band} / {preview.band_count}</span>
      <span className="inline-flex items-center gap-2"><span className="h-1.5 w-12 rounded-sm bg-gradient-to-r from-[#303030] to-white" aria-hidden="true" />
        <span>{preview.value_range ? preview.value_range.map(value => valueFormat.format(value)).join(' – ') : '没有有效预览像元'}</span>
        {preview.value_range && <span className="text-ink-400">预览值域</span>}
      </span>
      <span>{preview.nodata === null ? 'NoData 未声明' : `NoData ${preview.nodata}`}</span>
      {preview.masked_pixels > 0 && <span className="text-ink-400">棋盘格表示无效或覆盖范围外</span>}
    </figcaption>
  </figure>;
}
