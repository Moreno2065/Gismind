// Runtime artifacts retain their physical CRS. AMap receives only this separate
// GCJ02 display copy; no inference from coordinate magnitudes and no network calls.
import type { MapLayer } from '@/types/message';
import type { BBox } from './utils';

type SourceCRS = 'wgs84_lon_lat' | 'wgs84_lat_lon' | 'mercator' | 'gcj02';
type JsonRecord = Record<string, unknown>;
export interface RuntimeMapDisplay { layers: MapLayer[]; bbox: BBox; crs: { kind: 'GCJ02' } }

function record(value: unknown, message: string): JsonRecord {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new Error(message);
  return value as JsonRecord;
}

function sourceCRS(value: unknown): SourceCRS {
  const crs = record(value, '地图缺少明确的 CRS，无法安全显示');
  const keys = Object.keys(crs);
  if (crs.kind === 'GCJ02' && keys.length === 1) return 'gcj02';
  if (crs.kind === 'EPSG' && keys.length === 2 && keys.includes('code')) {
    if (crs.code === 4326) return 'wgs84_lon_lat';
    if (crs.code === 3857) return 'mercator';
  }
  if (crs.kind === 'WGS84' && keys.every((key) => key === 'kind' || key === 'axis_order')) {
    if (crs.axis_order === undefined || crs.axis_order === 'lon_lat') return 'wgs84_lon_lat';
    if (crs.axis_order === 'lat_lon') return 'wgs84_lat_lon';
  }
  throw new Error('地图 CRS 不支持或格式无效；仅支持 WGS84、EPSG:4326、EPSG:3857 和 GCJ02');
}

// Same explicit mainland bounding rectangle, ellipsoid and public mathematical
// convention as backend app/tools/geo_transform.py. Outside it AMap uses WGS84.
function wgs84ToGCJ02(lon: number, lat: number): [number, number] {
  if (lon < 73.66 || lon > 135.05 || lat < 3.86 || lat > 53.55) return [lon, lat];
  const x = lon - 105, y = lat - 35, pi = Math.PI;
  const wave = (20 * Math.sin(6 * x * pi) + 20 * Math.sin(2 * x * pi)) * 2 / 3;
  let dy = -100 + 2 * x + 3 * y + .2 * y * y + .1 * x * y + .2 * Math.sqrt(Math.abs(x));
  dy += wave + (20 * Math.sin(y * pi) + 40 * Math.sin(y / 3 * pi)) * 2 / 3
    + (160 * Math.sin(y / 12 * pi) + 320 * Math.sin(y * pi / 30)) * 2 / 3;
  let dx = 300 + x + 2 * y + .1 * x * x + .1 * x * y + .1 * Math.sqrt(Math.abs(x));
  dx += wave + (20 * Math.sin(x * pi) + 40 * Math.sin(x / 3 * pi)) * 2 / 3
    + (150 * Math.sin(x / 12 * pi) + 300 * Math.sin(x / 30 * pi)) * 2 / 3;
  const rad = lat * pi / 180, eccentricity = .00669342162296594323, axis = 6378245;
  const magic = 1 - eccentricity * Math.sin(rad) ** 2, root = Math.sqrt(magic);
  return [lon + dx * 180 / (axis / root * Math.cos(rad) * pi),
    lat + dy * 180 / ((axis * (1 - eccentricity)) / (magic * root) * pi)];
}

function position(value: unknown, crs: SourceCRS): number[] {
  if (!Array.isArray(value) || value.length < 2 || !value.every((item) => typeof item === 'number' && Number.isFinite(item))) {
    throw new Error('地图坐标必须包含有限数值的 x、y；附加维度也必须是有限数值');
  }
  let [lon, lat] = value as number[];
  if (crs === 'mercator') {
    const radius = 6378137, limit = Math.PI * radius;
    if (Math.abs(lon) > limit || Math.abs(lat) > limit) throw new Error('EPSG:3857 坐标超出 Web Mercator 世界范围');
    // The validated endpoints can round to 180.00000000000003 degrees.
    lon = Math.max(-180, Math.min(180, lon / radius * 180 / Math.PI));
    lat = Math.atan(Math.sinh(lat / radius)) * 180 / Math.PI;
  } else if (crs === 'wgs84_lat_lon') [lon, lat] = [lat, lon];
  if (lon < -180 || lon > 180 || lat < -90 || lat > 90) throw new Error('地图坐标不在声明 CRS 的有效经纬度范围内');
  const pair = crs === 'gcj02' ? [lon, lat] : wgs84ToGCJ02(lon, lat);
  return [...pair, ...value.slice(2)];
}

function copy(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(copy);
  if (value !== null && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, copy(item)]));
  return value;
}

function bounds(vertices: number[][]): BBox {
  if (!vertices.length) throw new Error('地图没有可显示的坐标');
  const result: BBox = [Infinity, Infinity, -Infinity, -Infinity];
  for (const [x, y] of vertices) {
    result[0] = Math.min(result[0], x); result[1] = Math.min(result[1], y);
    result[2] = Math.max(result[2], x); result[3] = Math.max(result[3], y);
  }
  return result;
}

export function projectRuntimeMapLayer(payload: unknown, artifactCRS: unknown): RuntimeMapDisplay {
  const map = record(payload, '地图数据格式无效');
  const crs = sourceCRS(map.crs);
  if (crs !== sourceCRS(artifactCRS)) throw new Error('地图 payload 与 Artifact 的 CRS 不一致');
  if (!Array.isArray(map.bbox) || map.bbox.length !== 4 || !map.bbox.every((item) => typeof item === 'number' && Number.isFinite(item))
    || map.bbox[0] > map.bbox[2] || map.bbox[1] > map.bbox[3]) throw new Error('地图 bbox 必须是有序的四个有限数值');
  if (!Array.isArray(map.layers)) throw new Error('地图图层格式无效');
  const vertices: number[][] = [];
  const depths: Record<string, number> = { Point: 0, MultiPoint: 1, LineString: 1, MultiLineString: 2, Polygon: 2, MultiPolygon: 3 };
  const transform = (value: unknown, depth: number): unknown => {
    if (!depth) { const projected = position(value, crs); vertices.push(projected); return projected; }
    if (!Array.isArray(value)) throw new Error('地图几何坐标嵌套格式无效');
    return value.map((item) => transform(item, depth - 1));
  };
  const checkNestedCRS = (value: JsonRecord) => {
    if ('crs' in value && sourceCRS(value.crs) !== crs) throw new Error('地图内嵌 CRS 与 Artifact 不一致');
  };
  const layers = map.layers.map((rawLayer) => {
    const layer = record(rawLayer, '地图图层格式无效');
    if (layer.type !== 'FeatureCollection' || !Array.isArray(layer.features)) throw new Error('Runtime 地图暂不支持此图层；仅支持 FeatureCollection');
    checkNestedCRS(layer);
    const layerStart = vertices.length;
    const features = layer.features.map((rawFeature) => {
      const feature = record(rawFeature, '地图要素格式无效');
      if (feature.type !== 'Feature') throw new Error('地图要素格式无效');
      checkNestedCRS(feature);
      const geometry = record(feature.geometry, '地图几何坐标格式无效');
      if (typeof geometry.type !== 'string' || !Object.prototype.hasOwnProperty.call(depths, geometry.type)) throw new Error('地图暂不支持几何类型：' + String(geometry.type));
      checkNestedCRS(geometry);
      const start = vertices.length;
      const converted = { ...copy(geometry) as JsonRecord, coordinates: transform(geometry.coordinates, depths[geometry.type]) };
      const featureCopy = { ...copy(feature) as JsonRecord, geometry: converted };
      const local = vertices.slice(start);
      if ('bbox' in converted) converted.bbox = bounds(local);
      if ('bbox' in featureCopy) featureCopy.bbox = bounds(local);
      if ('crs' in converted) converted.crs = { kind: 'GCJ02' };
      if ('crs' in featureCopy) featureCopy.crs = { kind: 'GCJ02' };
      return featureCopy;
    });
    const display = { ...copy(layer) as JsonRecord, features };
    if ('bbox' in display) display.bbox = bounds(vertices.slice(layerStart));
    if ('crs' in display) display.crs = { kind: 'GCJ02' };
    return display;
  });
  return { layers: layers as unknown as MapLayer[], bbox: bounds(vertices), crs: { kind: 'GCJ02' } };
}
