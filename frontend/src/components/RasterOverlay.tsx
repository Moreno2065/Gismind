// frontend/src/components/RasterOverlay.tsx
// AMap ImageOverlay 包装组件 — 负责栅格图层的创建与销毁生命周期。

import { useEffect, useRef } from 'react';
import type { RasterLayer } from '@/types/message';

interface RasterOverlayProps {
  map: any; // AMap.Map instance
  AMap: any; // namespace returned by the shared loader
  layer: RasterLayer;
  onError?: (err: Error) => void;
  onReady?: (overlay: any) => void;
  onDispose?: (overlay: any) => void;
}

export function RasterOverlay({ map, AMap, layer, onError, onReady, onDispose }: RasterOverlayProps) {
  const overlayRef = useRef<any>(null);

  useEffect(() => {
    if (!map || !AMap) return;

    try {
      const dataUrl = `data:image/png;base64,${layer.png_b64}`;
      const [minx, miny, maxx, maxy] = layer.bbox;
      const sw = new AMap.LngLat(minx, miny);
      const ne = new AMap.LngLat(maxx, maxy);
      const bounds = new AMap.Bounds(sw, ne);

      const overlay = new AMap.ImageLayer({
        url: dataUrl,
        bounds,
        opacity: layer.opacity ?? 0.7,
      });

      overlay.on('error', (e: any) => {
        onError?.(new Error(e?.message ?? 'ImageOverlay load failed'));
      });

      map.add(overlay);
      overlayRef.current = overlay;
      onReady?.(overlay);

      // 如果 bbox 超出当前视野，自动定位
      map.setBounds(bounds, false, [30, 30, 30, 30]);
    } catch (err) {
      onError?.(err as Error);
    }

    return () => {
      if (overlayRef.current) {
        const overlay = overlayRef.current;
        try { map.remove(overlay); } catch { /* ignore */ }
        onDispose?.(overlay);
        overlayRef.current = null;
      }
    };
  }, [AMap, map, layer, onDispose, onError, onReady]);

  return null;
}
