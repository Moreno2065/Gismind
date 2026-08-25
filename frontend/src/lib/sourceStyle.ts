// frontend/src/lib/sourceStyle.ts
// 数据源视觉隔离 — Amap 橙色高亮，OSM 灰色半透明，Upload 中性。
// 给地图要素（点/线/面）按 _source 计算样式。

import type { POISource } from '@/types/message';

export interface SourceStyle {
  color: string; // 主色
  glow: string; // 高光
  opacity: number; // 填充透明度
  label: string; // 中文标签
  badge: 'amber' | 'osm' | 'neutral';
}

const MAP: Record<POISource, SourceStyle> = {
  Amap: {
    color: '#ff7a1a',
    glow: '#ff9a47',
    opacity: 0.9,
    label: '高德',
    badge: 'amber',
  },
  OSM_CN: {
    color: '#9ca3af',
    glow: '#6b7280',
    opacity: 0.55,
    label: 'OSM·CN',
    badge: 'osm',
  },
  OSM_Global: {
    color: '#9ca3af',
    glow: '#6b7280',
    opacity: 0.55,
    label: 'OSM',
    badge: 'osm',
  },
  Upload: {
    color: '#7dd3fc',
    glow: '#38bdf8',
    opacity: 0.8,
    label: '上传',
    badge: 'neutral',
  },
};

export function sourceStyle(src?: POISource | string | null): SourceStyle {
  if (src && src in MAP) return MAP[src as POISource];
  // 未知来源默认中性
  return {
    color: '#8a96a6',
    glow: '#5b6776',
    opacity: 0.6,
    label: '数据',
    badge: 'neutral',
  };
}

/** 从 GeoJSON feature properties 提取 _source */
export function featureSource(feature: { properties?: Record<string, unknown> }): POISource | undefined {
  return feature.properties?._source as POISource | undefined;
}
