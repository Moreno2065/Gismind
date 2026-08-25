// frontend/src/components/RasterOverlay.tsx
// AMap ImageOverlay 包装组件 — 负责栅格图层的创建与销毁生命周期。

import { useEffect, useRef } from 'react';
import type { RasterLayer } from '@/types/message';

interface RasterOverlayProps {
  map: any; // AMap.Map instance
  layer: RasterLayer;
  onError?: (err: Error) => void;
}

export function RasterOverlay({ map, layer, onError }: RasterOverlayProps) {
  const overlayRef = useRef<any>(null);

  useEffect(() => {
    if (!map || !window.AMap) return;

    try {
      const dataUrl = `data:image/png;base64,${layer.png_b64}`;
      const [minx, miny, maxx, maxy] = layer.bbox;
      const sw = new window.AMap.LngLat(minx, miny);
      const ne = new window.AMap.LngLat(maxx, maxy);
      const bounds = new window.AMap.Bounds(sw, ne);

      const overlay = new window.AMap.ImageOverlay({
        url: dataUrl,
        bounds: bounds,
        opacity: layer.opacity ?? 0.7,
      });

      overlay.on('error', (e: any) => {
        onError?.(new Error(e?.message ?? 'ImageOverlay load failed'));
      });

      map.add(overlay);
      overlayRef.current = overlay;

      // 如果 bbox 超出当前视野，自动定位
      map.setFitView([overlay], false, [30, 30]);
    } catch (err) {
      onError?.(err as Error);
    }

    return () => {
      if (overlayRef.current) {
        try { map.remove(overlayRef.current); } catch { /* ignore */ }
        overlayRef.current = null;
      }
    };
  }, [map, layer, onError]);

  return null;
}
