// frontend/src/components/LazyMapView.tsx
// 地图懒加载与内存管理（核心难点）：
//   - IntersectionObserver 监听视口可见性
//   - 可见时用 @amap/amap-jsapi-loader 加载高德地图，渲染 FeatureCollection
//     （支持 Polygon 含孔洞、Point、LineString、Multi*）
//   - 不可见时 destroy 释放内存，显示"地图已休眠"占位
//   - 右上角全屏展开按钮 → FullscreenMap

import { useCallback, useEffect, useRef, useState } from 'react';
import { loadAMap } from '@/lib/amap';
import { renderLayersOnMap } from '@/lib/mapRenderers';
import { sourceStyle, featureSource } from '@/lib/sourceStyle';
import { countFeatures, normalizeBbox } from '@/lib/utils';
import { RasterOverlay } from './RasterOverlay';
import type { MapLayer, RasterLayer } from '@/types/message';
import { FullscreenMap } from './FullscreenMap';

interface LazyMapViewProps {
  layers: MapLayer[];
  bbox: [number, number, number, number];
  expired?: boolean;
  featureCount?: number;
  /** Called before the map container height may change — used for scroll preservation. */
  onBeforeLoad?: () => void;
  /** Called after the map container height has settled. */
  onAfterLoad?: () => void;
}

const PLACEHOLDER_HEIGHT = 380;

export function LazyMapView({ layers, bbox, expired, featureCount, onBeforeLoad, onAfterLoad }: LazyMapViewProps) {
  const safeBbox = normalizeBbox(bbox);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<any>(null);
  const AMapRef = useRef<any>(null);
  const overlaysRef = useRef<any[]>([]);
  const layersRef = useRef(layers);
  const bboxRef = useRef(safeBbox);
  const prevLayersJsonRef = useRef(JSON.stringify(layers));
  const [isVisible, setIsVisible] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [fullscreen, setFullscreen] = useState(false);

  // 保持 ref 为最新值，供稳定回调使用
  layersRef.current = layers;
  bboxRef.current = safeBbox;

  // 1. 监听是否进入视口
  useEffect(() => {
    const node = containerRef.current;
    if (!node) return;
    const observer = new IntersectionObserver(
      ([entry]) => setIsVisible(entry.isIntersecting),
      { threshold: 0.15, rootMargin: '60px' },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  // 2. 渲染 layers 到地图实例（稳定回调，通过 ref 读取最新 props）
  const renderLayers = useCallback((map: any, AMap: any) => {
    // 清理旧覆盖物
    for (const o of overlaysRef.current) {
      try {
        o.setMap?.(null);
      } catch {
        /* noop */
      }
    }
    overlaysRef.current = [];

    // raster 层由 RasterOverlay 组件处理，不在此处渲染
    const nonRasterLayers = layersRef.current.filter((l) => l.type !== 'raster');
    const newOverlays = renderLayersOnMap(map, AMap, nonRasterLayers, bboxRef.current);
    overlaysRef.current = newOverlays;
  }, []);

  // 3. 视口可见时加载地图，不可见时销毁
  useEffect(() => {
    if (isVisible) {
      if (mapRef.current) return; // 已加载
      setLoading(true);
      setLoadError(null);
      onBeforeLoad?.();
      let cancelled = false;
      loadAMap()
        .then((AMap: any) => {
          if (cancelled || !containerRef.current) return;
          AMapRef.current = AMap;
          const map = new AMap.Map(containerRef.current, {
            zoom: 13,
            mapStyle: 'amap://styles/dark', // 暗色底图契合整体配色
            resizeEnable: true,
            features: ['bg', 'road', 'building'],
          });
          mapRef.current = map;
          renderLayers(map, AMap);
          setLoading(false);
          onAfterLoad?.();
        })
        .catch((err: Error) => {
          if (cancelled) return;
          setLoadError(err.message || '地图加载失败');
          setLoading(false);
          onAfterLoad?.();
        });
      return () => {
        cancelled = true;
      };
    }
    // 不可见 — 销毁释放内存
    if (mapRef.current) {
      for (const o of overlaysRef.current) {
        try {
          o.setMap?.(null);
        } catch {
          /* noop */
        }
      }
      overlaysRef.current = [];
      try {
        mapRef.current.destroy();
      } catch {
        /* noop */
      }
      mapRef.current = null;
      AMapRef.current = null;
    }
    return undefined;
  }, [isVisible, renderLayers, onBeforeLoad, onAfterLoad]);

  // 4. layers 实际变化时增量重绘（深比较避免父级无意义重渲染导致刷新）
  useEffect(() => {
    if (!mapRef.current || !AMapRef.current) return;
    const json = JSON.stringify(layers);
    if (json === prevLayersJsonRef.current) return;
    prevLayersJsonRef.current = json;
    renderLayers(mapRef.current, AMapRef.current);
  }, [layers, renderLayers]);

  // 卸载时清理
  useEffect(() => {
    return () => {
      if (mapRef.current) {
        try {
          mapRef.current.destroy();
        } catch {
          /* noop */
        }
        mapRef.current = null;
      }
    };
  }, []);

  const hasHeatmap = layers.some((l) => l.type === 'heatmap');

  if (expired) {
    return (
      <div
        className="relative mt-3 overflow-hidden rounded-lg border border-ink-700 bg-ink-900"
        style={{ height: PLACEHOLDER_HEIGHT }}
      >
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-ink-900 p-6 text-center">
          <div className="font-mono text-xs text-signal-error">地图数据已过期，请重新查询</div>
          {featureCount != null && (
            <div className="text-sm text-ink-400">曾包含 {featureCount} 个要素</div>
          )}
          <div className="mt-1 font-mono text-[10px] text-ink-500">
            bbox [{safeBbox[0].toFixed(2)}, {safeBbox[1].toFixed(2)}, {safeBbox[2].toFixed(2)}, {safeBbox[3].toFixed(2)}]
          </div>
        </div>
      </div>
    );
  }

  return (
    <>
      <div
        className="relative mt-3 overflow-hidden rounded-lg border border-ink-700 bg-ink-900"
        style={{ height: PLACEHOLDER_HEIGHT }}
      >
        {/* 地图容器 */}
        <div ref={containerRef} className="absolute inset-0" />

        {/* 栅格图层 — 使用 React 组件管理 ImageOverlay 生命周期 */}
        {isVisible && !loading && !loadError && mapRef.current && (
          layers.filter((l): l is RasterLayer => l.type === 'raster').map((layer, i) => (
            <RasterOverlay key={i} map={mapRef.current} layer={layer} onError={console.warn} />
          ))
        )}

        {/* 加载骨架屏 */}
        {loading && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-paper/80 backdrop-blur-sm">
            <div className="h-10 w-10 animate-spin rounded-full border-2 border-ink-600 border-t-amber" />
            <div className="font-mono text-xs text-ink-300">正在生成空间分析地图…</div>
            <SkeletonGrid />
          </div>
        )}

        {/* 加载错误 */}
        {loadError && !loading && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-ink-900 p-6 text-center">
            <div className="font-mono text-xs text-signal-error">地图加载失败</div>
            <div className="max-w-md text-sm text-ink-400">{loadError}</div>
          </div>
        )}

        {/* 休眠占位 — 仅在不可见且未加载时显示 */}
        {!isVisible && !loading && !loadError && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-ink-900">
            <SleepingMapIcon />
            <div className="font-mono text-xs text-ink-400">地图已休眠，滚动至此加载</div>
            <div className="mt-1 font-mono text-[10px] text-ink-500">
              {countFeatures(layers)} features · bbox [{safeBbox[0].toFixed(2)}, {safeBbox[1].toFixed(2)}, {safeBbox[2].toFixed(2)}, {safeBbox[3].toFixed(2)}]
            </div>
          </div>
        )}

        {/* 右上角全屏按钮 */}
        {isVisible && !loading && !loadError && (
          <button
            type="button"
            onClick={() => setFullscreen(true)}
            className="absolute right-3 top-3 z-10 flex h-9 w-9 items-center justify-center rounded-md border border-ink-600 bg-paper/90 text-ink-200 backdrop-blur-sm transition hover:border-amber hover:text-amber"
            title="全屏展开"
            aria-label="全屏展开地图"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3" />
            </svg>
          </button>
        )}

        {/* 数据源图例 */}
        {isVisible && !loading && !loadError && <SourceLegend layers={layers} />}

        {/* 热力图不支持提示 */}
        {hasHeatmap && (
          <div className="absolute left-3 top-3 z-10 rounded-md border border-ink-600 bg-paper/90 px-2.5 py-1.5 backdrop-blur-sm">
            <span className="font-mono text-[11px] text-signal-fetching">热力图暂不支持</span>
          </div>
        )}
      </div>

      {fullscreen && (
        <FullscreenMap layers={layers} bbox={safeBbox} onClose={() => setFullscreen(false)} />
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// 视觉子组件
// ---------------------------------------------------------------------------

function SkeletonGrid() {
  return (
    <div
      className="mt-4 h-24 w-72 opacity-40"
      style={{
        backgroundImage:
          'linear-gradient(rgba(255,122,26,0.15) 1px, transparent 1px), linear-gradient(90deg, rgba(255,122,26,0.15) 1px, transparent 1px)',
        backgroundSize: '24px 24px',
        maskImage: 'linear-gradient(90deg, transparent, #000 50%, transparent)',
      }}
    />
  );
}

function SleepingMapIcon() {
  return (
    <svg width="48" height="48" viewBox="0 0 48 48" fill="none" className="opacity-40">
      <circle cx="24" cy="20" r="12" stroke="#5b6776" strokeWidth="1.5" fill="none" />
      <path d="M24 14 V20 L29 22" stroke="#5b6776" strokeWidth="1.5" strokeLinecap="round" fill="none" />
      <path d="M8 38 Q24 30 40 38" stroke="#3a4554" strokeWidth="1" fill="none" strokeLinecap="round" />
      <path d="M34 10 l2 2 l4 -4" stroke="#ff7a1a" strokeWidth="1.5" strokeLinecap="round" fill="none" opacity="0.7" />
    </svg>
  );
}

function SourceLegend({ layers }: { layers: MapLayer[] }) {
  const sources = new Set<string>();
  for (const l of layers) {
    if (l.type === 'FeatureCollection') {
      for (const f of l.features) {
        const s = featureSource(f);
        if (s) sources.add(s);
      }
    } else if (l.type === 'point') {
      sources.add(l.source);
    }
  }
  if (sources.size === 0) return null;
  return (
    <div className="absolute bottom-3 left-3 z-10 flex flex-col gap-1 rounded-md border border-ink-600 bg-paper/90 p-2 backdrop-blur-sm">
      {Array.from(sources).map((s) => {
        const st = sourceStyle(s);
        return (
          <div key={s} className="flex items-center gap-2">
            <span
              className="inline-block h-2 w-2 rounded-full"
              style={{ background: st.color, boxShadow: `0 0 6px ${st.glow}` }}
            />
            <span className="font-mono text-[10px] text-ink-300">{st.label}</span>
          </div>
        );
      })}
    </div>
  );
}
