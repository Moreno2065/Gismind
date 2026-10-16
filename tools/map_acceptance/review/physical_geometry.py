"""Read all final MapLayer-associated GPKGs using independent GDAL/OSR + GCJ02.

Only downloaded GPKG/Map/result files are read. No runtime database, browser,
network, model, application coordinate implementation, or earlier pilot replay.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import time

TOOL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOL))
from sdk_geometry_oracle import verify_sdk_geometry


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load(path):
    with Path(path).open(encoding='utf-8') as stream:
        return json.load(stream)


def gcj02(position):
    lng, lat = position
    if not (73.66 <= lng <= 135.05 and 3.86 <= lat <= 53.55):
        return [lng, lat]
    x, y = lng - 105, lat - 35
    common = (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    north = -100 + 2*x + 3*y + .2*y*y + .1*x*y + .2*math.sqrt(abs(x)) + common
    north += (20*math.sin(y*math.pi) + 40*math.sin(y*math.pi/3))*2/3
    north += (160*math.sin(y*math.pi/12) + 320*math.sin(y*math.pi/30))*2/3
    east = 300 + x + 2*y + .1*x*x + .1*x*y + .1*math.sqrt(abs(x)) + common
    east += (20*math.sin(x*math.pi) + 40*math.sin(x*math.pi/3))*2/3
    east += (150*math.sin(x*math.pi/12) + 300*math.sin(x*math.pi/30))*2/3
    radians = math.radians(lat)
    eccentricity = .00669342162296594323
    factor = 1 - eccentricity*math.sin(radians)**2
    north *= 180 / (6378245*(1-eccentricity)/(factor*math.sqrt(factor))*math.pi)
    east *= 180 / (6378245/math.sqrt(factor)*math.cos(radians)*math.pi)
    return [lng + east, lat + north]


def coordinates_transform(value, transform):
    if len(value) >= 2 and type(value[0]) in (int, float):
        require(len(value) == 2 and all(math.isfinite(v) for v in value), 'Only finite 2D positions are accepted')
        return transform(value)
    return [coordinates_transform(child, transform) for child in value]


def read_physical(path):
    from osgeo import gdal, osr
    gdal.UseExceptions()
    dataset = gdal.OpenEx(str(path), gdal.OF_VECTOR | gdal.OF_READONLY, allowed_drivers=['GPKG'])
    require(dataset is not None and dataset.GetLayerCount() == 1, 'Expected one physical GPKG layer')
    layer = dataset.GetLayer(0)
    source = layer.GetSpatialRef()
    require(source is not None, 'Missing physical CRS')
    source = source.Clone()
    source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    target = osr.SpatialReference()
    target.ImportFromEPSG(4326)
    target.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = osr.CoordinateTransformation(source, target)
    expected, geographic = [], []
    for feature in layer:
        geometry = feature.GetGeometryRef()
        require(geometry is not None and not geometry.IsEmpty() and geometry.IsValid(), 'Invalid/empty physical geometry')
        projected = geometry.Clone()
        require(projected.Transform(transform) == 0, 'GDAL projection failed')
        value = json.loads(projected.ExportToJson())
        require(value['type'] in ('Point', 'Polygon'), 'Unsupported physical geometry type')
        if value['type'] == 'Polygon':
            require(len(value['coordinates']) == 1 and len(value['coordinates'][0]) in (65, 129), 'Unexpected physical ring/part structure')
        geographic.append({'type': 'Feature', 'properties': {}, 'geometry': value})
        expected.append({'type': 'Feature', 'properties': {}, 'geometry': {
            'type': value['type'], 'coordinates': coordinates_transform(value['coordinates'], gcj02)}})
    require(bool(expected), 'Empty physical feature set')
    metadata = {'layer_name': layer.GetName(), 'physical_crs_wkt': source.ExportToWkt(), 'feature_count': len(expected)}
    layer, dataset = None, None
    return geographic, expected, metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    original = args.original.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    spec = importlib.util.spec_from_file_location('physical_binding_helpers', TOOL / 'audit_rendered_results.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    rows = [json.loads(line) for line in (original / 'results.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    require(len(rows) == len({row['case_id'] for row in rows}) == 2000, 'Original cohort is not 2000 unique cases')
    counts, failures, seen_maps = Counter(), [], set()
    maximum_map_error = 0.0
    started = time.perf_counter()
    with (args.output / 'physical-features.jsonl').open('x', encoding='utf-8') as output:
        for row in rows:
            try:
                case_root = Path(row['evidence_path']).resolve()
                require(case_root.parent.parent == original, 'Original case outside requested root')
                case = helper.bind_case(row, load(case_root / 'result.json'), case_root)
                if case.get('excluded'):
                    counts['excluded_rasters'] += 1
                    continue
                gpkg = case['gpkg_path']
                siblings_before = sorted(p.name for p in case_root.glob(gpkg.name + '*'))
                before = sha(gpkg)
                geographic, expected, metadata = read_physical(gpkg)
                map_features = load(case['map_path'])['layers'][0]['features']
                mapping = verify_sdk_geometry(geographic, [{'kind': f['geometry']['type'], 'coordinates': f['geometry']['coordinates']} for f in map_features], tolerance_degrees=2e-6)
                require(sha(gpkg) == before == case['file_artifact']['content_hash'], 'Physical GPKG changed while reading')
                require(sorted(p.name for p in case_root.glob(gpkg.name + '*')) == siblings_before, 'GDAL created a source sidecar')
                require(case['map_artifact']['artifact_id'] not in seen_maps, 'Duplicate final MapLayer')
                seen_maps.add(case['map_artifact']['artifact_id'])
                record = {key: case[key] for key in ('case_id', 'shard', 'run_id', 'session_id')}
                record.update({'map_artifact_id': case['map_artifact']['artifact_id'], 'gpkg_artifact_id': case['file_artifact']['artifact_id'],
                    'map_sha256': case['map_artifact']['content_hash'], 'gpkg_sha256': before,
                    'original_result_sha256': case['result_sha256'], 'features': expected,
                    'physical_feature_count': metadata['feature_count'], 'map_wgs84_max_error_degrees': mapping['max_error_degrees']})
                output.write(json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n')
                output.flush()
                counts['maps'] += 1
                counts['features'] += len(expected)
                for feature in expected:
                    kind = feature['geometry']['type']
                    counts['geometry_' + kind] += 1
                    if kind == 'Point':
                        counts['positions'] += 1
                    else:
                        counts['rings'] += len(feature['geometry']['coordinates'])
                        counts['positions'] += sum(len(ring) for ring in feature['geometry']['coordinates'])
                maximum_map_error = max(maximum_map_error, mapping['max_error_degrees'])
                if counts['maps'] % 100 == 0:
                    print(json.dumps({'physical_maps': counts['maps'], 'elapsed_seconds': round(time.perf_counter()-started, 2)}), flush=True)
            except Exception as error:
                failures.append({'case_id': row['case_id'], 'error': type(error).__name__ + ': ' + str(error)[:800]})
                print(json.dumps(failures[-1], ensure_ascii=False), flush=True)
    result = {'passed': counts['maps'] == 1800 and counts['excluded_rasters'] == 200 and not failures,
        'counts': dict(counts), 'failures': failures, 'map_wgs84_max_error_degrees': maximum_map_error,
        'tolerance_degrees': 2e-6, 'elapsed_seconds': time.perf_counter()-started,
        'method': 'GDAL read-only GPKG geometry/CRS -> independent OSR EPSG4326 -> standalone explicit GCJ02; final Map WGS84 every vertex/ring/feature matched',
        'input_root': str(original), 'features_path': str((args.output / 'physical-features.jsonl').resolve()),
        'no_runtime_database_or_network': True, 'old_pilot_data_reused': False}
    with (args.output / 'physical-review.json').open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
