// frontend/src/lib/utils.ts
// Shared utility functions used across components.

import type { MapLayer } from '@/types/message';

export type BBox = [number, number, number, number];

/** Keep malformed/legacy map payloads from crashing map components. */
export function normalizeBbox(value: unknown): BBox {
  if (
    Array.isArray(value)
    && value.length === 4
    && value.every((item) => typeof item === 'number' && Number.isFinite(item))
  ) {
    return value as BBox;
  }
  return [0, 0, 0, 0];
}

/** Count the total number of geographic features across a MapLayer array. */
export function countFeatures(layers: MapLayer[]): number {
  let n = 0;
  for (const l of layers) {
    if (l.type === 'FeatureCollection') n += l.features.length;
    else if (l.type === 'point') n += l.coordinates.length;
    else if (l.type === 'polygon') n += l.coordinates.length;
    else if (l.type === 'polyline') n += l.coordinates.length;
    else if (l.type === 'heatmap') n += l.coordinates.length;
  }
  return n;
}
