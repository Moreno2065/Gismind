// frontend/src/lib/mapRenderers.ts
// 高德地图覆盖物渲染辅助 — 被 LazyMapView 与 FullscreenMap 共享。
// 支持 GeoJSON Feature 的 Point/MultiPoint/LineString/MultiLineString/Polygon/MultiPolygon。

import { sourceStyle, featureSource } from './sourceStyle';
import type { GeoJSONFeature, MapLayer } from '@/types/message';

export interface AMapOverlayLike {
  setMap(map: any): void;
}

// Reuse a single InfoWindow to avoid leaking instances on every marker click.
let _sharedInfoWindow: any = null;

function getSharedInfoWindow(AMap: any, content: string, offset?: any): any {
  if (!_sharedInfoWindow) {
    _sharedInfoWindow = new AMap.InfoWindow({ content, offset });
  }
  _sharedInfoWindow.setContent(content);
  return _sharedInfoWindow;
}

/** 把单个 GeoJSON Feature 转 AMap 覆盖物（统一返回带 setMap 的对象） */
export function featureToOverlay(
  AMap: any,
  feature: GeoJSONFeature,
  layerStyle?: Record<string, unknown>,
): AMapOverlayLike | null {
  const src = featureSource(feature);
  const st = sourceStyle(src);
  const geom = feature.geometry;
  if (!geom) return null;

  const props = feature.properties ?? {};
  const popup = buildPopup(props);

  const makeMarker = (coord: number[]) => {
    const m = new AMap.Marker({
      position: [coord[0], coord[1]],
      content: markerDom(st.color, st.glow),
      offset: new AMap.Pixel(-8, -8),
      anchor: 'center',
    });
    if (popup) {
      m.on('click', () => {
        const info = getSharedInfoWindow(AMap, popup, new AMap.Pixel(0, -16));
        info.open(m.getMap() ?? null, m.getPosition());
      });
    }
    return m;
  };

  if (geom.type === 'Point') {
    return makeMarker(geom.coordinates as number[]);
  }

  if (geom.type === 'MultiPoint') {
    const markers = (geom.coordinates as number[][]).map((c) => makeMarker(c));
    return {
      setMap: (map: any) => markers.forEach((m) => m.setMap(map)),
    };
  }

  if (geom.type === 'LineString') {
    const path = (geom.coordinates as number[][]).map((c) => [c[0], c[1]]);
    return new AMap.Polyline({
      path,
      strokeColor: (layerStyle?.stroke_color as string) ?? st.color,
      strokeWeight: (layerStyle?.stroke_width as number) ?? 3,
      strokeOpacity: 0.9,
      lineJoin: 'round',
    });
  }

  if (geom.type === 'MultiLineString') {
    const polylines = (geom.coordinates as number[][][]).map((line) =>
      new AMap.Polyline({
        path: line.map((c) => [c[0], c[1]]),
        strokeColor: (layerStyle?.stroke_color as string) ?? st.color,
        strokeWeight: (layerStyle?.stroke_width as number) ?? 3,
        strokeOpacity: 0.9,
        lineJoin: 'round',
      }),
    );
    return {
      setMap: (map: any) => polylines.forEach((p) => p.setMap(map)),
    };
  }

  if (geom.type === 'Polygon') {
    const paths = (geom.coordinates as number[][][]).map((ring) => ring.map((c) => [c[0], c[1]]));
    return new AMap.Polygon({
      path: paths,
      fillColor: (layerStyle?.fill_color as string) ?? st.color,
      fillOpacity: (layerStyle?.fill_opacity as number) ?? st.opacity * 0.4,
      strokeColor: (layerStyle?.stroke_color as string) ?? st.color,
      strokeWeight: (layerStyle?.stroke_width as number) ?? 1.5,
    });
  }

  if (geom.type === 'MultiPolygon') {
    const polygons = (geom.coordinates as number[][][][]).map((poly) =>
      new AMap.Polygon({
        path: poly.map((ring) => ring.map((c) => [c[0], c[1]])),
        fillColor: (layerStyle?.fill_color as string) ?? st.color,
        fillOpacity: (layerStyle?.fill_opacity as number) ?? st.opacity * 0.4,
        strokeColor: (layerStyle?.stroke_color as string) ?? st.color,
        strokeWeight: (layerStyle?.stroke_width as number) ?? 1.5,
      }),
    );
    return {
      setMap: (map: any) => polygons.forEach((p) => p.setMap(map)),
    };
  }

  return null;
}

export function markerDom(color: string, glow: string): string {
  return `<div style="width:16px;height:16px;border-radius:50%;background:${color};box-shadow:0 0 0 3px ${glow}33,0 0 8px ${color}aa;"></div>`;
}

export function buildPopup(props: Record<string, unknown>): string | null {
  const keys = Object.keys(props).filter((k) => k !== '_source' && props[k] != null);
  if (keys.length === 0) return null;
  const rows = keys
    .map(
      (k) =>
        `<tr><td style="padding:2px 8px 2px 0;color:#8a96a6;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11px;">${escapeHtml(k)}</td><td style="padding:2px 0;color:#dde3eb;font-size:12px;">${escapeHtml(String(props[k]))}</td></tr>`,
    )
    .join('');
  return `<div style="background:#0f141b;border:1px solid #2a3340;border-radius:6px;padding:8px;max-width:280px;"><table>${rows}</table></div>`;
}

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c] as string));
}

/**
 * Render all MapLayers onto an existing AMap instance and fit the map to the given bbox.
 * Returns the array of created overlays (for lifecycle management / cleanup).
 * Shared between LazyMapView and FullscreenMap to avoid duplicated overlay creation logic.
 */
export function renderLayersOnMap(
  map: any,
  AMap: any,
  layers: MapLayer[],
  bbox: [number, number, number, number],
  options?: { padding?: number[] },
): any[] {
  const overlays: any[] = [];

  for (const layer of layers) {
    if (layer.type === 'FeatureCollection') {
      for (const feature of layer.features) {
        try {
          const overlay = featureToOverlay(AMap, feature, layer.style);
          if (overlay) {
            overlay.setMap(map);
            overlays.push(overlay);
          }
        } catch (e) {
          console.warn('Failed to render feature:', e);
        }
      }
    } else if (layer.type === 'point') {
      const st = sourceStyle(layer.source);
      for (const coord of layer.coordinates) {
        try {
          const m = new AMap.Marker({
            position: coord,
            content: markerDom(st.color, st.glow),
            offset: new AMap.Pixel(-8, -8),
            anchor: 'center',
          });
          m.setMap(map);
          overlays.push(m);
        } catch (e) {
          console.warn('Failed to render point marker:', e);
        }
      }
    } else if (layer.type === 'polygon') {
      for (const polygon of layer.coordinates) {
        try {
          const paths = polygon.map((ring: number[][]) => ring.map((c) => [c[0], c[1]]));
          const p = new AMap.Polygon({
            path: paths,
            fillColor: layer.fill_color ?? '#ff7a1a',
            fillOpacity: layer.fill_opacity ?? 0.25,
            strokeColor: (layer.style?.stroke_color as string | undefined) ?? '#ff7a1a',
            strokeWidth: (layer.style?.stroke_width as number | undefined) ?? 1.5,
          });
          p.setMap(map);
          overlays.push(p);
        } catch (e) {
          console.warn('Failed to render polygon:', e);
        }
      }
    } else if (layer.type === 'polyline') {
      for (const line of layer.coordinates) {
        try {
          const path = line.map((c) => [c[0], c[1]]);
          const pl = new AMap.Polyline({
            path,
            strokeColor: layer.stroke_color ?? '#FF6B35',
            strokeWeight: layer.stroke_width ?? 4,
            lineJoin: 'round',
          });
          pl.setMap(map);
          overlays.push(pl);
        } catch (e) {
          console.warn('Failed to render polyline:', e);
        }
      }
    } else if (layer.type === 'raster') {
      // Raster layer via AMap ImageOverlay
      try {
        const dataUrl = `data:image/png;base64,${layer.png_b64}`;
        const [minx, miny, maxx, maxy] = layer.bbox;
        const sw = new AMap.LngLat(minx, miny);
        const ne = new AMap.LngLat(maxx, maxy);
        const bounds = new AMap.Bounds(sw, ne);
        const overlay = new AMap.ImageOverlay({
          url: dataUrl,
          bounds,
          opacity: layer.opacity ?? 0.7,
        });
        overlay.setMap(map);
        overlays.push(overlay);
      } catch (e) {
        console.warn('Failed to create ImageOverlay for raster layer:', e);
      }
    }
    // heatmap is shown as a placeholder card in the UI, skip rendering here
  }

  // Fit map to bbox
  try {
    const pad = options?.padding ?? [60, 60, 60, 60];
    const bounds = new AMap.Bounds([bbox[0], bbox[1]], [bbox[2], bbox[3]]);
    map.setBounds(bounds, false, pad);
  } catch {
    /* bbox 异常时忽略 */
  }

  return overlays;
}
