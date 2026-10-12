import { describe, expect, it } from 'vitest';
import { projectRuntimeMapLayer } from '@/lib/runtimeMapProjection';

const wgs = { kind: 'EPSG', code: 4326 };
const gcj = { kind: 'GCJ02' };
const mercator = { kind: 'EPSG', code: 3857 };
const R = 6378137;
function map(coordinates: unknown = [118.8, 32.05], crs: unknown = wgs, type = 'Point') {
  return { crs, bbox: [0, 0, 1, 1], layers: [{ type: 'FeatureCollection', features: [
    { type: 'Feature', id: 'source-id', geometry: { type, coordinates }, properties: { name: '地块', nested: { count: 4 } } },
  ] }] };
}
function geometry(display: ReturnType<typeof projectRuntimeMapLayer>) {
  const layer = display.layers[0];
  if (layer.type !== 'FeatureCollection') throw new Error('expected FeatureCollection');
  return layer.features[0].geometry;
}

describe('Runtime display-only map projection', () => {
  // Fresh references obtained independently from unchanged backend
  // app.tools.geo_transform.wgs84_to_gcj02, including its inclusive China bounds.
  it.each([
    [[116.397, 39.908], [116.40324337854781, 39.90940335390724]],
    [[118.8, 32.05], [118.80517716491906, 32.04792177186948]],
    [[121.4737, 31.2304], [121.47822305927693, 31.22845773757727]],
    [[73.66, 3.86], [73.66278451577757, 3.859348557624419]],
    [[135.05, 53.55], [135.05952103770557, 53.552490668163074]],
  ])('matches the current backend reference for %j', (input, reference) => {
    const result = geometry(projectRuntimeMapLayer(map(input), wgs)).coordinates as number[];
    expect(result[0]).toBeCloseTo(reference[0], 10);
    expect(result[1]).toBeCloseTo(reference[1], 10);
  });

  it.each([[0, 0], [-74, 40.7], [73.659, 32], [118, 53.551]])('does not offset coordinates outside the backend China bounds: %j', (x, y) => {
    expect(geometry(projectRuntimeMapLayer(map([x, y]), wgs)).coordinates).toEqual([x, y]);
  });

  it('uses analytic Web Mercator inverse and then the matching GCJ02 conversion', () => {
    const metres = [R * 118.8 * Math.PI / 180, R * Math.log(Math.tan(Math.PI / 4 + 32.05 * Math.PI / 360))];
    const result = geometry(projectRuntimeMapLayer(map(metres, mercator), mercator)).coordinates as number[];
    expect(result[0]).toBeCloseTo(118.80517716491906, 9);
    expect(result[1]).toBeCloseTo(32.04792177186948, 9);
    const known = geometry(projectRuntimeMapLayer(map([0, R * Math.log(1 + Math.sqrt(2))], mercator), mercator)).coordinates as number[];
    expect(known[0]).toBe(0);
    expect(known[1]).toBeCloseTo(45, 10);
  });

  it('never offsets already-GCJ02 data again', () => {
    const input = [118.80517716491906, 32.04792177186948];
    const once = projectRuntimeMapLayer(map(input, gcj), gcj);
    expect(geometry(once).coordinates).toEqual(input);
    expect(projectRuntimeMapLayer(once, once.crs)).toEqual(once);
  });

  it.each([-1, 1])('accepts the exact Mercator world longitude edge %s without accepting out-of-world metres', (sign) => {
    const edge = sign * Math.PI * R;
    expect(geometry(projectRuntimeMapLayer(map([edge, 0], mercator), mercator)).coordinates).toEqual([sign * 180, 0]);
    expect(() => projectRuntimeMapLayer(map([edge + sign, 0], mercator), mercator)).toThrow(/坐标/);
  });

  it('accepts WGS84 lon_lat as EPSG4326 equivalent but honors explicit lat_lon', () => {
    const equivalent = projectRuntimeMapLayer(map(), { kind: 'WGS84', axis_order: 'lon_lat' });
    const latLon = { kind: 'WGS84', axis_order: 'lat_lon' };
    const swapped = projectRuntimeMapLayer(map([32.05, 118.8], latLon), latLon);
    expect(geometry(swapped).coordinates).toEqual(geometry(equivalent).coordinates);
    expect(() => projectRuntimeMapLayer(map([32.05, 118.8], latLon), wgs)).toThrow(/CRS.*不一致/);
  });

  it.each([
    ['MultiPoint', [[1, 2, 3], [4, 5, 6]]],
    ['LineString', [[1, 2, 3], [4, 5, 6]]],
    ['MultiLineString', [[[1, 2, 3], [4, 5, 6]], [[7, 8, 9], [10, 11, 12]]]],
    ['Polygon', [[[1, 2, 3], [4, 2, 3], [4, 5, 3], [1, 2, 3]], [[2, 3, 4], [3, 3, 4], [3, 4, 4], [2, 3, 4]]]],
    ['MultiPolygon', [[[[1, 2, 3], [4, 2, 3], [4, 5, 3], [1, 2, 3]]], [[[7, 8, 9], [10, 8, 9], [10, 11, 9], [7, 8, 9]]]]],
  ])('preserves %s nesting, rings, holes, z, IDs and properties in a deep display copy', (type, coordinates) => {
    const original = map(coordinates, wgs, type as string);
    const before = JSON.stringify(original);
    const display = projectRuntimeMapLayer(original, wgs);
    expect(geometry(display).coordinates).toEqual(coordinates);
    expect(JSON.stringify(original)).toBe(before);
    const layer = display.layers[0];
    if (layer.type !== 'FeatureCollection') throw new Error('expected FeatureCollection');
    expect(layer.features[0].id).toBe('source-id');
    expect(layer.features[0].properties).toEqual(original.layers[0].features[0].properties);
    (layer.features[0].properties!.nested as { count: number }).count = 99;
    expect(original.layers[0].features[0].properties.nested.count).toBe(4);
  });

  it('recomputes the displayed bbox from transformed vertices, not a stale source bbox', () => {
    const original = map([[118.8, 32.05, 7], [121.4737, 31.2304, 11]], wgs, 'MultiPoint');
    const display = projectRuntimeMapLayer(original, wgs);
    expect(display.bbox).toEqual([118.80517716491906, 31.22845773757727, 121.47822305927693, 32.04792177186948]);
    expect(original.bbox).toEqual([0, 0, 1, 1]);
    expect(geometry(display).coordinates).toEqual([[118.80517716491906, 32.04792177186948, 7], [121.47822305927693, 31.22845773757727, 11]]);
  });

  it.each([undefined, null, {}, { kind: 'EPSG', code: 32650 }, { kind: 'EPSG', code: '4326' }, { kind: 'BD09' }, { kind: 'WGS84', axis_order: 'guess' }])('rejects missing/unsupported CRS %j without coordinate guessing', (crs) => {
    expect(() => projectRuntimeMapLayer(map([118, 32], crs), crs)).toThrow(/CRS/);
  });

  it('rejects missing payload CRS even when the Artifact has CRS, and vice versa', () => {
    const payload = map(); delete (payload as { crs?: unknown }).crs;
    expect(() => projectRuntimeMapLayer(payload, wgs)).toThrow(/CRS/);
    expect(() => projectRuntimeMapLayer(map(), undefined)).toThrow(/CRS/);
    expect(() => projectRuntimeMapLayer(map(), gcj)).toThrow(/CRS.*不一致/);
  });

  it.each(['layer', 'feature', 'geometry'])('rejects conflicting CRS on a nested %s', (level) => {
    const payload = map([118.80517716491906, 32.04792177186948]);
    const layer = payload.layers[0], feature = layer.features[0];
    Object.assign(level === 'layer' ? layer : level === 'feature' ? feature : feature.geometry, { crs: gcj });
    expect(() => projectRuntimeMapLayer(payload, wgs)).toThrow(/CRS.*不一致/);
  });

  it.each(['layer', 'feature', 'geometry'])('updates a matching nested %s CRS in the display copy only', (level) => {
    const payload = map();
    const layer = payload.layers[0], feature = layer.features[0];
    const source = level === 'layer' ? layer : level === 'feature' ? feature : feature.geometry;
    Object.assign(source, { crs: wgs });
    const display = projectRuntimeMapLayer(payload, wgs);
    const outputLayer = display.layers[0];
    if (outputLayer.type !== 'FeatureCollection') throw new Error('expected FeatureCollection');
    const outputFeature = outputLayer.features[0];
    const output = level === 'layer' ? outputLayer : level === 'feature' ? outputFeature : outputFeature.geometry;
    expect(output).toHaveProperty('crs', gcj);
    expect(source).toHaveProperty('crs', wgs);
    expect(projectRuntimeMapLayer(display, gcj)).toEqual(display);
  });

  it.each([[0, 0, Infinity, 1], [0, NaN, 1, 1], [1, 0, 0, 1], [0, 1, 1, 0], [0, 1, 2]])('rejects malformed source bbox %j', (bbox) => {
    expect(() => projectRuntimeMapLayer({ ...map(), bbox }, wgs)).toThrow(/bbox/);
  });

  it.each([[Infinity, 32], [118, NaN], [118, 32, Infinity], ['118', 32], [13000000, 3700000]])('rejects nonfinite/nongeographic coordinates %j', (coordinates) => {
    expect(() => projectRuntimeMapLayer(map(coordinates), wgs)).toThrow(/坐标/);
  });

  it('rejects out-of-world Mercator and unsupported collections/layers rather than silently dropping them', () => {
    expect(() => projectRuntimeMapLayer(map([1e30, 1e30], mercator), mercator)).toThrow(/坐标/);
    expect(() => projectRuntimeMapLayer(map([], wgs, 'GeometryCollection'), wgs)).toThrow(/GeometryCollection/);
    expect(() => projectRuntimeMapLayer({ ...map(), layers: [{ type: 'raster', bbox: [0, 0, 1, 1] }] }, wgs)).toThrow(/图层/);
  });
});
