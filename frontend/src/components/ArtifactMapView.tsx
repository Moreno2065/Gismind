import { useEffect, useRef, useState } from 'react';
import { getRuntimeArtifact, getRuntimeArtifactContent } from '@/api/client';
import { projectRuntimeMapLayer } from '@/lib/runtimeMapProjection';
import type { MapBlock } from '@/types/message';
import { LazyMapView } from './LazyMapView';

// The local cache drops heavy geometry, not the durable Artifact identity.
export function ArtifactMapView({ block, onBeforeLoad, onAfterLoad }: {
  block: MapBlock; onBeforeLoad?: () => void; onAfterLoad?: () => void;
}) {
  const [loaded, setLoaded] = useState<ReturnType<typeof projectRuntimeMapLayer> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  async function restore() {
    if (!block.artifact_id || loading) return;
    const controller = new AbortController(); request.current?.abort(); request.current = controller;
    setLoading(true); setError(null);
    try {
      const metadata = await getRuntimeArtifact(block.artifact_id, controller.signal);
      const content = await getRuntimeArtifactContent(block.artifact_id, controller.signal);
      const projected = projectRuntimeMapLayer(content, metadata.crs);
      if (!controller.signal.aborted) setLoaded(projected);
    } catch (cause) {
      if (!controller.signal.aborted) setError((cause as Error).message);
    } finally { if (!controller.signal.aborted) setLoading(false); }
  }
  if (!block.expired || loaded) return <LazyMapView layers={loaded?.layers ?? block.layers} bbox={loaded?.bbox ?? block.bbox} onBeforeLoad={onBeforeLoad} onAfterLoad={onAfterLoad} />;
  return <div className="my-3 rounded-xl border border-ink-600 bg-ink-900 p-5">
    <div className="mb-2 font-mono text-[10px] uppercase tracking-[.18em] text-amber">PERSISTED MAP</div>
    <p className="text-sm text-ink-300">{block.artifact_id ? '地图几何未保留在本地缓存，可从原始成果重新加载，无需重跑任务。' : '这条历史地图没有成果引用，无法恢复。'}</p>
    {error && <p role="alert" className="mt-2 break-words text-sm text-signal-error">{error}</p>}
    {block.artifact_id && <button type="button" disabled={loading} onClick={() => void restore()} className="mt-3 rounded-lg border border-amber/40 px-3 py-2 text-sm text-amber disabled:opacity-50">{loading ? '正在读取成果…' : error ? '重试加载历史地图' : '加载历史地图'}</button>}
  </div>;
}
