// frontend/src/components/FullscreenMap.tsx
// 全屏地图弹窗 — 接收相同 layers 数据，支持更完整交互。
// 复用 LazyMapView 的渲染逻辑但始终加载（无懒加载/无休眠）。

import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { loadAMap } from '@/lib/amap';
import { renderLayersOnMap } from '@/lib/mapRenderers';
import type { MapLayer } from '@/types/message';

interface FullscreenMapProps {
  layers: MapLayer[];
  bbox: [number, number, number, number];
  onClose: () => void;
}

export function FullscreenMap({ layers, bbox, onClose }: FullscreenMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<any>(null);
  const overlaysRef = useRef<any[]>([]);
  const [error, setError] = useState<string | null>(null);

  // Esc 关闭
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    // 锁定 body 滚动
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  useEffect(() => {
    let cancelled = false;
    loadAMap()
      .then((AMap: any) => {
        if (cancelled || !containerRef.current) return;
        const map = new AMap.Map(containerRef.current, {
          zoom: 13,
          mapStyle: 'amap://styles/dark',
          resizeEnable: true,
          features: ['bg', 'road', 'building', 'point'],
        });
        map.addControl(new AMap.Scale());
        map.addControl(new AMap.ToolBar({ position: 'RB' }));
        mapRef.current = map;

        overlaysRef.current = renderLayersOnMap(map, AMap, layers, bbox, { padding: [80, 80, 80, 80] });
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message || '地图加载失败');
      });

    return () => {
      cancelled = true;
      for (const o of overlaysRef.current) {
        try {
          o.setMap?.(null);
        } catch {
          /* noop */
        }
      }
      overlaysRef.current = [];
      if (mapRef.current) {
        try {
          mapRef.current.destroy();
        } catch {
          /* noop */
        }
        mapRef.current = null;
      }
    };
  }, [layers, bbox]);

  const hasHeatmap = layers.some((l) => l.type === 'heatmap');

  return createPortal(
    <div className="fixed inset-0 z-50 flex flex-col bg-ink-950 backdrop-blur-md animate-fade-in">
      <div className="flex items-center justify-between border-b border-ink-700 px-5 py-3">
        <div className="flex items-baseline gap-3">
          <span className="font-display text-lg text-ink-100">空间分析地图</span>
          <span className="font-mono text-[11px] text-ink-400">
            bbox [{bbox[0].toFixed(3)}, {bbox[1].toFixed(3)}, {bbox[2].toFixed(3)}, {bbox[3].toFixed(3)}]
          </span>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="flex h-9 items-center gap-2 rounded-md border border-ink-600 px-3 text-sm text-ink-200 transition hover:border-signal-error hover:text-signal-error"
          aria-label="关闭全屏"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M18 6 6 18M6 6l12 12" />
          </svg>
          <span className="font-mono text-xs">Esc</span>
        </button>
      </div>
      <div className="relative flex-1">
        <div ref={containerRef} className="absolute inset-0" />
        {error && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-ink-900 text-center">
            <div className="font-mono text-xs text-signal-error">地图加载失败</div>
            <div className="max-w-md text-sm text-ink-400">{error}</div>
          </div>
        )}

        {hasHeatmap && (
          <div className="absolute left-4 top-4 z-10 rounded-md border border-ink-600 bg-paper/90 px-3 py-2 backdrop-blur-sm">
            <span className="font-mono text-[11px] text-signal-fetching">热力图暂不支持</span>
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}

