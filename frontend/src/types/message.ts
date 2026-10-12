// Frontend presentation types for Runtime artifacts and message projections.

import type { RuntimeEvent } from '@/types/runtime';

/** Data-source metadata used to render POI results consistently. */
export type POISource = 'Amap' | 'OSM_CN' | 'OSM_Global' | 'Upload';

export interface PointLayer {
  type: 'point';
  coordinates: [number, number][];
  source: POISource;
  popup_fields?: string[];
  style?: Record<string, unknown>;
}

export interface HeatmapLayer {
  type: 'heatmap';
  coordinates: [number, number][];
  weights?: number[];
  radius?: number;
  gradient?: Record<string, string>;
  style?: Record<string, unknown>;
}

export interface PolygonLayer {
  type: 'polygon';
  coordinates: number[][][][];
  fill_color?: string;
  fill_opacity?: number;
  style?: Record<string, unknown>;
}

export interface PolylineLayer {
  type: 'polyline';
  coordinates: number[][][];
  stroke_color?: string;
  stroke_width?: number;
  style?: Record<string, unknown>;
}

export type GeoJSONGeometryType =
  | 'Point'
  | 'MultiPoint'
  | 'LineString'
  | 'MultiLineString'
  | 'Polygon'
  | 'MultiPolygon'
  | 'GeometryCollection';

export interface GeoJSONGeometry {
  type: GeoJSONGeometryType;
  coordinates: number[] | number[][] | number[][][] | number[][][][];
}

export interface GeoJSONFeature {
  type: 'Feature';
  geometry: GeoJSONGeometry;
  properties?: Record<string, unknown> & { _source?: POISource };
  id?: string | number;
}

export interface FeatureCollectionLayer {
  type: 'FeatureCollection';
  features: GeoJSONFeature[];
  style?: Record<string, unknown>;
}

export interface RasterLayer {
  type: 'raster';
  png_b64: string;
  bbox: [number, number, number, number];
  width: number;
  height: number;
  value_range?: [number, number];
  colormap?: string;
  value_kind?: string;
  opacity?: number;
}

export type MapLayer =
  | PointLayer
  | HeatmapLayer
  | PolygonLayer
  | PolylineLayer
  | FeatureCollectionLayer
  | RasterLayer;

export interface TextBlock {
  type: 'text';
  content: string;
  artifact_id?: string;
}

export interface MapBlock {
  type: 'map';
  artifact_id?: string;
  layers: MapLayer[];
  bbox: [number, number, number, number];
  expired?: boolean;
  featureCount?: number;
}

export interface ChartBlock {
  type: 'chart';
  config: Record<string, unknown>;
}

export type MessageBlock = TextBlock | MapBlock | ChartBlock;
export type ChatRole = 'user' | 'assistant';
export type MessageStatus = 'thinking' | 'fetching' | 'summarizing' | 'reviewing' | 'reflecting' | 'done' | 'error';

/** A local presentation of persisted Runtime facts; it is never an execution source. */
export interface ChatMessage {
  id: string;
  run_id?: string;
  role: ChatRole;
  blocks: MessageBlock[];
  status: MessageStatus;
  statusMessage?: string;
  executionTrace?: RuntimeEvent[];
  trace_id?: string;
  error?: { code: string; message: string };
  created_at: number;
}
