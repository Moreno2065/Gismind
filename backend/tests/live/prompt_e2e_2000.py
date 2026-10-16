"""Opt-in public API -> real provider -> isolated worker -> artifact suite.

Run from backend: python -m tests.live.prompt_e2e_2000 --help
Offline unit tests and --inventory do not execute or count as live E2E.
Expected semantics stay in this module and never enter a planner request.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
import random
import threading
import time
import zipfile

from .prompt_corpus_2000 import Case, CORPUS_SHA256, SEED, VERSION, corpus_digest, load_cases

RUNNER_VERSION = 'real-api-artifact-v5'
ORACLE_VERSION = 'terminal-spatial-semantics-v2'
RUNNER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_APP_CONSTRUCTION_LOCK = threading.Lock()


def runner_digest():
    return RUNNER_SHA256


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def runtime_fingerprint(source_root, settings, *, prompt_version, case_timeout):
    """Hash executable source and an explicit allowlist of nonsecret settings."""
    source_root = Path(source_root)
    sources = [(p.relative_to(source_root).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest())
        for p in sorted(source_root.rglob('*.py')) if p.is_file()]
    if not sources:
        raise ValueError('runtime source fingerprint requires Python source files')
    names = (
        'LLM_MODEL', 'LLM_TIMEOUT', 'LLM_MAX_TOKENS', 'LLM_TEMPERATURE',
        'APP_PLANNER_MAX_TOKENS', 'APP_PLANNER_TIMEOUT_S', 'APP_MAX_LLM_RETRIES',
        'APP_SANDBOX_ENABLED', 'APP_SANDBOX_TIMEOUT_S', 'APP_SANDBOX_MEMORY_MB',
        'APP_SANDBOX_NETWORK_ALLOWLIST', 'UPLOAD_MAX_SIZE',
        'UPLOAD_ZIP_MAX_TOTAL_SIZE', 'UPLOAD_ZIP_MAX_FILE_COUNT',
        'AMAP_QPS_LIMIT', 'AMAP_TIMEOUT', 'AMAP_MAX_RETRY',
        'OSM_TIMEOUT', 'OSM_MAX_RETRY',
    )
    public = {name: getattr(settings, name, None) for name in names}
    public.update(prompt_version=prompt_version, case_timeout_seconds=case_timeout,
        effective_transport_max_retries=0, worker_isolate=True,
        provider_endpoint_hash=hashlib.sha256(str(getattr(settings, 'LLM_BASE_URL', '')).encode()).hexdigest())
    source_hash = hashlib.sha256(canonical(sources).encode('utf-8')).hexdigest()
    config_hash = hashlib.sha256(canonical(public).encode('utf-8')).hexdigest()
    return dict(fingerprint=hashlib.sha256((source_hash + config_hash).encode()).hexdigest(),
        source_sha256=source_hash, config_sha256=config_hash, source_file_count=len(sources), config=public)


def validate_runtime_manifest(journal, current):
    if any(r.get('type') == 'runtime_drift' for r in journal.records):
        raise ValueError('runtime drift invalidated this checkpoint; use a new output directory')
    manifests = [r for r in journal.records if r.get('type') == 'provider_manifest']
    if manifests and manifests[0].get('runtime', {}).get('fingerprint') != current['fingerprint']:
        raise ValueError('runtime source/config fingerprint changed; use a new output directory')


def check_runtime_batch(journal, baseline, read_current):
    try:
        current = read_current()
    except Exception as exc:
        current = dict(fingerprint=None, error=type(exc).__name__)
    if current['fingerprint'] == baseline['fingerprint']:
        return True
    journal.append(dict(type='runtime_drift', expected=baseline['fingerprint'],
        observed=current.get('fingerprint'), diagnostic=current.get('error'),
        reason='production source or nonsecret execution configuration changed'))
    return False


def require_provider(config):
    missing = [name for name in ('LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL') if not str(config.get(name, '')).strip()]
    if missing:
        raise ValueError('Live E2E requires ' + ', '.join(missing))


def request_payload(case, session_id, artifact_ids):
    return dict(session_id=session_id, message=case.prompt, input_artifact_ids=artifact_ids, client_request_id=case.case_id)


class Journal:
    """Fsync each event, recover only a torn final line, reject corpus drift."""
    def __init__(self, path, digest):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.records = []
        if self.path.exists():
            raw = self.path.read_bytes()
            lines = raw.splitlines(keepends=True)
            offset = 0
            for index, line in enumerate(lines):
                try:
                    item = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    if index != len(lines) - 1 or line.endswith(b'\n'):
                        raise ValueError('corrupt checkpoint record in middle of journal')
                    # Preserve the torn fragment for investigation.
                    self.path.with_suffix('.torn').write_bytes(line)
                    with self.path.open('r+b') as stream:
                        stream.truncate(offset)
                        stream.flush()
                        os.fsync(stream.fileno())
                    break
                if not isinstance(item, dict):
                    raise ValueError('checkpoint record must be an object')
                self.records.append(item)
                offset += len(line)
            if self.records and self.records[0].get('corpus_sha256') != digest:
                raise ValueError('checkpoint corpus digest mismatch')
            if self.records and (self.records[0].get('runner_version') != RUNNER_VERSION or self.records[0].get('runner_sha256') != runner_digest()):
                raise ValueError('checkpoint runner version/digest mismatch; use a new output directory')
            if self.records and raw and not raw.endswith(b'\n') and len(self.records) == len(lines):
                with self.path.open('ab') as stream:
                    stream.write(b'\n')
        if not self.records:
            self.append(dict(type='manifest', corpus_sha256=digest, corpus_version=VERSION, seed=SEED,
                runner_version=RUNNER_VERSION, runner_sha256=runner_digest()))

    @property
    def completed(self):
        return {r['case_id']: r for r in self.records if r.get('type') == 'result'}

    def append(self, item):
        with self.lock:
            record = dict(item, recorded_at=datetime.now().astimezone().isoformat())
            with self.path.open('ab') as stream:
                stream.write((canonical(record) + '\n').encode('utf-8'))
                stream.flush()
                os.fsync(stream.fileno())
            self.records.append(record)

    def for_case(self, case_id):
        with self.lock:
            return [dict(r) for r in self.records if r.get('case_id') == case_id]


class BudgetExhausted(RuntimeError):
    pass


class CallBudget:
    """Reserve conservative upper bounds before calls, including after crashes.

    Tokens/cost are reservations, not provider invoices. USD pricing is supplied
    by the operator. Unknown usage is never represented as zero actual usage.
    """
    def __init__(self, journal, *, max_calls, max_tokens, max_cost, reserve_tokens, usd_per_million, min_interval):
        self.journal = journal
        self.max_calls, self.max_tokens, self.max_cost = max_calls, max_tokens, max_cost
        self.reserve_tokens, self.rate, self.interval = reserve_tokens, usd_per_million, min_interval
        self.lock = threading.Lock()
        self.next_call = 0.0

    def reserve(self, case_id, *, calls=1, tokens=None):
        with self.lock:
            previous = [r for r in self.journal.records if r.get('type') == 'call_reserved']
            count = sum(r['calls'] for r in previous)
            reserved = sum(r['tokens'] for r in previous)
            tokens = max(self.reserve_tokens, tokens or 0) * calls
            if count + calls > self.max_calls or reserved + tokens > self.max_tokens or (reserved + tokens) * self.rate / 1_000_000 > self.max_cost:
                self.journal.append(dict(type='budget_denied', case_id=case_id))
                raise BudgetExhausted('provider-call/token/estimated-cost reservation limit reached')
            delay = max(0.0, self.next_call - time.monotonic())
            if delay:
                time.sleep(delay)
            self.next_call = time.monotonic() + self.interval * calls
            self.journal.append(dict(type='call_reserved', case_id=case_id, calls=calls, tokens=tokens, estimated_cost_usd=tokens * self.rate / 1_000_000))


def source_features(case, second=False):
    from shapely.geometry import MultiPolygon, Point, Polygon, box, mapping
    features = []
    for index in range(case.count):
        x = 13000000 + case.profile * 2300 + index * 500 + (100 if second else 0)
        y = 3700000 + case.profile * 1100 + (index % 3) * 400
        shape = Polygon([(x, y), (x + 100, y), (x + 80, y + 100), (x + 20, y + 120)])
        if case.family in ('buffer', 'hull', 'dedup', 'missing_distance'):
            shape = Point(x, y)
        if case.family == 'explode':
            shape = MultiPolygon([box(x, y, x + 80, y + 80), box(x + 150, y, x + 230, y + 80)])
        if case.family == 'clip' and second:
            shape = box(x - 100, y - 10, x - 40, y + 130)
        props = {'编号': index, '名称': f'地块{index}', '数值': (index + 1) * 10, '类别': '甲' if index % 2 else '乙'}
        features.append(dict(type='Feature', geometry=mapping(shape), properties=props))
    return features * 2 if case.family == 'dedup' else features


def raster_values(case):
    import numpy as np
    values = np.arange(case.count ** 2, dtype='float32').reshape(case.count, case.count) + case.profile
    values[0, 0] = -9999
    return values


def build_uploads(case, root):
    """Build actual decodable upload bytes; no Artifact is pre-published."""
    allowed_formats = ({"csv"} if case.family in {"csvpoints", "bad_csv"} else
                       {"tif"} if case.family == "rastercalc" else
                       {"geojson"} if case.family == "broken_vector" else {"geojson", "gpkg", "zip"})
    if case.file_format not in allowed_formats:
        raise ValueError(f"unsupported upload format: {case.file_format}")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    crs = dict(kind='EPSG', code=3857)
    if case.family in ('csvpoints', 'bad_csv'):
        records = ['经度,纬度,编号,数值']
        for i in range(case.count):
            lat = 100 + case.profile if case.family == 'bad_csv' and i == 0 else 32 + i * .001
            records.append(f'{118 + case.profile * .01 + i * .001:.6f},{lat:.6f},{i},{(i+1)*10}')
        return [('点位.csv', ('\n'.join(records) + '\n').encode('utf-8'), dict(kind='EPSG', code=4326))]
    if case.family == 'broken_vector':
        return [('损坏.geojson', ('{"type":"FeatureCollection","features":[' + ',' * case.count).encode('utf-8'), crs)]
    if case.family == 'rastercalc':
        import rasterio
        from rasterio.io import MemoryFile
        from rasterio.transform import from_origin
        with MemoryFile() as memory:
            with memory.open(driver='GTiff', width=case.count, height=case.count, count=1, dtype='float32', crs='EPSG:3857', transform=from_origin(13000000, 3700000, 100, 100), nodata=-9999) as dataset:
                dataset.write(raster_values(case), 1)
            return [('高程.tif', memory.read(), crs)]
    result = []
    for index in range(2 if case.family in ('clip', 'merge') else 1):
        features = source_features(case, second=bool(index))
        name = f'图层{index + 1}'
        if case.file_format == 'geojson':
            payload = dict(type='FeatureCollection', features=features, crs=dict(type='name', properties=dict(name='EPSG:3857')))
            data = canonical(payload).encode('utf-8')
        else:
            import geopandas as gpd
            frame = gpd.GeoDataFrame.from_features(features, crs='EPSG:3857')
            if case.file_format == 'gpkg':
                path = root / f'{name}.gpkg'
                frame.to_file(path, driver='GPKG', engine='pyogrio')
                data = path.read_bytes()
            else:
                folder = root / name
                folder.mkdir(exist_ok=True)
                frame.to_file(folder / 'layer.shp', driver='ESRI Shapefile', encoding='UTF-8', engine='pyogrio')
                memory = io.BytesIO()
                with zipfile.ZipFile(memory, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                    for path in sorted(folder.iterdir()):
                        archive.writestr(path.name, path.read_bytes())
                data = memory.getvalue()
        result.append((f'{name}.{case.file_format}', data, crs))
    return result


def terminal_spatial_payload(artifact):
    """Interpret only the supplied terminal delivery, never search intermediates."""
    from shapely.geometry import shape
    from shapely.ops import unary_union
    if artifact['type'] in ('Vector', 'Raster'):
        return artifact
    assert artifact['type'] in ('File', 'MapLayer'), 'unsupported terminal artifact type'
    payload = json.loads(artifact['payload'])
    assert isinstance(payload, dict), 'terminal spatial payload must be an object'
    if artifact['type'] == 'MapLayer':
        assert payload.get('crs') == artifact.get('crs'), 'MapLayer CRS differs from Artifact'
        layers = payload.get('layers')
        assert isinstance(layers, list) and len(layers) == 1, 'MapLayer must deliver exactly one spatial layer'
        collection = layers[0]
        assert isinstance(collection, dict) and collection.get('type') == 'FeatureCollection', 'MapLayer must embed its actual FeatureCollection'
        if payload.get('bbox') is not None:
            features = collection.get('features')
            assert isinstance(features, list) and features, 'empty MapLayer'
            bounds = unary_union([shape(f['geometry']) for f in features]).bounds
            assert len(payload['bbox']) == 4 and all(math.isclose(x, y, rel_tol=0, abs_tol=1e-7)
                for x, y in zip(payload['bbox'], bounds)), 'MapLayer bbox differs from delivered geometry'
        payload = collection
    else:
        # GeoJSON with no physical/embedded CRS uses RFC 7946 longitude/latitude.
        if not payload.get('crs') and not payload.get('_physical_crs'):
            assert artifact.get('crs') == dict(kind='EPSG', code=4326), 'GeoJSON File without CRS must use WGS84'
    assert payload.get('type') == 'FeatureCollection' and isinstance(payload.get('features'), list), 'terminal delivery is not a FeatureCollection'
    return dict(artifact, type='Vector', payload=canonical(payload).encode('utf-8'))


def assert_semantics(case, artifacts):
    """Every terminal delivery must independently satisfy the same oracle."""
    assert artifacts, 'missing final spatial artifact'
    for artifact in artifacts:
        _assert_semantic_artifact(case, [terminal_spatial_payload(artifact)])


def _assert_semantic_artifact(case, artifacts):
    """Numerical oracle uses source geometry and standard libraries, no handlers."""
    from shapely.geometry import Point, shape
    from shapely.ops import transform, unary_union
    from pyproj import Geod, Transformer
    if case.family == 'rastercalc':
        import numpy as np
        from rasterio.io import MemoryFile
        rasters = [a for a in artifacts if a['type'] == 'Raster']
        assert rasters, 'missing final Raster artifact'
        assert rasters[-1].get('crs') == dict(kind='EPSG', code=3857), 'Raster Artifact CRS differs from source'
        with MemoryFile(rasters[-1]['payload']) as memory, memory.open() as result:
            expected = raster_values(case)
            mask = expected == -9999
            assert result.crs.to_epsg() == 3857
            assert result.shape == expected.shape
            assert result.count == 1, 'output band count differs'
            assert np.allclose(tuple(result.transform), (100, 0, 13000000, 0, -100, 3700000, 0, 0, 1), rtol=0, atol=1e-8), 'output raster grid changed'
            actual = result.read(1, masked=True)
            assert np.array_equal(np.ma.getmaskarray(actual), mask)
            assert np.allclose(actual.data[~mask], expected[~mask] + case.factor, rtol=1e-6)
        return
    vectors = [a for a in artifacts if a['type'] == 'Vector']
    assert vectors, 'missing final Vector artifact'
    artifact = vectors[-1]
    payload = json.loads(artifact['payload'])
    expected_crs = 4326 if case.family in ('csvpoints', 'reproject') else 3857
    assert artifact.get('crs') == dict(kind='EPSG', code=expected_crs), 'Vector Artifact CRS differs from requested result'
    if payload.get('_physical_crs'):
        assert payload['_physical_crs'].upper() == f'EPSG:{expected_crs}', 'payload physical CRS differs from Artifact'
    if payload.get('_crs_label'):
        assert payload['_crs_label'].upper() in (f'EPSG:{expected_crs}', 'WGS84' if expected_crs == 4326 else ''), 'payload semantic CRS differs from Artifact'
    if payload.get('crs'):
        from pyproj import CRS
        assert CRS.from_user_input(payload['crs']['properties']['name']).to_epsg() == expected_crs, 'embedded CRS differs'
    actual_features = payload.get('features', [])
    actual = [shape(f['geometry']) for f in actual_features]
    source = source_features(case)
    geometries = [shape(f['geometry']) for f in source]
    properties = [f['properties'] for f in actual_features]
    family = case.family
    property_sources = [f['properties'] for f in source]
    if family == 'filter':
        property_sources = [p for p in property_sources if p['数值'] > case.threshold]
    elif family == 'dedup':
        property_sources = property_sources[:case.count]
    elif family == 'explode':
        property_sources = [p for p in property_sources for _ in range(2)]
    elif family == 'merge':
        property_sources += [f['properties'] for f in source_features(case, second=True)]
    elif family == 'keep':
        property_sources = [{k: p[k] for k in ('编号', '名称')} for p in property_sources]
    elif family == 'rename':
        property_sources = [{('新名' if k == '名称' else k): v for k, v in p.items()} for p in property_sources]
    if family not in ('hull', 'dissolve', 'csvpoints', 'clip'):
        required = set(property_sources[0])
        assert all(required <= set(p) for p in properties), 'required original properties are missing'
        projected_properties = [{k: p[k] for k in required} for p in properties]
        assert sorted(map(canonical, projected_properties)) == sorted(map(canonical, property_sources)), 'original properties changed'
    assert actual and all(g.is_valid and not g.is_empty for g in actual), 'invalid or empty geometry'
    if family == 'csvpoints':
        expected = [Point(118 + case.profile * .01 + i * .001, 32 + i * .001) for i in range(case.count)]
    elif family == 'buffer':
        assert len(actual) == case.count and all(g.geom_type in ('Polygon', 'MultiPolygon') for g in actual)
        to_wgs = Transformer.from_crs(3857, 4326, always_xy=True).transform
        geod = Geod(ellps='WGS84')
        remaining = list(actual)
        for source_index, original in enumerate(geometries):
            polygon = min(remaining, key=lambda g: g.centroid.distance(original))
            remaining.remove(polygon)
            assert polygon.geom_type == 'Polygon' and not polygon.interiors, 'point buffer must be a polygon without holes'
            observed_properties = properties[actual.index(polygon)]
            assert all(observed_properties[k] == v for k, v in source[source_index]['properties'].items()), 'buffer geometry detached from its source record'
            assert polygon.covers(original)
            center = transform(to_wgs, original)
            geographic_polygon = transform(to_wgs, polygon)
            area, _ = geod.geometry_area_perimeter(geographic_polygon)
            assert math.isclose(abs(area), math.pi * case.distance ** 2, rel_tol=.025), 'buffer area differs from a circle'
            boundary = geographic_polygon.exterior.coords
            radii = [geod.inv(center.x, center.y, x, y)[2] for x, y in boundary]
            assert max(abs(r - case.distance) for r in radii) <= max(1.0, case.distance * .03)
        return
    elif family == 'centroid':
        expected = [g.centroid for g in geometries]
    elif family == 'hull':
        expected = [unary_union(geometries).convex_hull]
    elif family == 'bbox':
        expected = [g.envelope for g in geometries]
    elif family == 'explode':
        expected = [part for g in geometries for part in g.geoms]
    elif family == 'dedup':
        expected = geometries[:case.count]
    elif family == 'filter':
        expected = [g for g, f in zip(geometries, source) if f['properties']['数值'] > case.threshold]
        assert sorted(p['数值'] for p in properties) == sorted(f['properties']['数值'] for f in source if f['properties']['数值'] > case.threshold)
    elif family == 'keep':
        expected = geometries
        assert all(set(p) == {'编号', '名称'} for p in properties)
    elif family == 'rename':
        expected = geometries
        assert all('名称' not in p and '新名' in p for p in properties)
        assert sorted(p['新名'] for p in properties) == sorted(f['properties']['名称'] for f in source)
    elif family == 'calculate':
        expected = geometries
        to_utm = Transformer.from_crs(3857, 32650, always_xy=True).transform
        by_id = {f['properties']['编号']: g for f, g in zip(source, geometries)}
        assert len(properties) == len(source)
        for prop in properties:
            expected_value = transform(to_utm, by_id[prop['编号']]).area * case.factor
            assert math.isclose(float(prop['倍值']), expected_value, rel_tol=.02)
    elif family == 'reproject':
        expected = [transform(Transformer.from_crs(3857, 4326, always_xy=True).transform, g) for g in geometries]
        assert artifact.get('crs', {}).get('code') == 4326, 'output CRS metadata must match coordinates'
    elif family == 'dissolve':
        expected = [unary_union([g for g, f in zip(geometries, source) if f['properties']['类别'] == key]) for key in ('甲', '乙')]
        assert {p['类别'] for p in properties} == {'甲', '乙'}
    elif family == 'clip':
        mask = unary_union([shape(f['geometry']) for f in source_features(case, second=True)])
        expected = [g.intersection(mask) for g in geometries if g.intersects(mask)]
        property_sources = [f['properties'] for g, f in zip(geometries, source) if g.intersects(mask)]
    elif family == 'merge':
        expected = geometries + [shape(f['geometry']) for f in source_features(case, second=True)]
    else:
        raise AssertionError(f'no independent oracle for {family}')
    assert len(actual) == len(expected), f'feature count {len(actual)} != {len(expected)}'
    tolerance = 1e-7 if family in ('reproject', 'csvpoints') else .15
    expected_properties = property_sources
    if family == 'hull':
        expected_properties = [None]
    elif family == 'dissolve':
        expected_properties = [{'类别': key} for key in ('甲', '乙')]
    elif family == 'csvpoints':
        expected_properties = [{'编号': i, '数值': (i + 1) * 10} for i in range(case.count)]
    unmatched = list(zip(actual, properties))
    for index, geometry in enumerate(expected):
        candidate_index = min(range(len(unmatched)), key=lambda i: unmatched[i][0].hausdorff_distance(geometry))
        candidate, observed_properties = unmatched.pop(candidate_index)
        wanted_properties = expected_properties[index]
        if wanted_properties is not None:
            if family == 'clip':
                # Intersection may legitimately retain the mask's attributes as
                # well, suffixing the entire source schema. Require one complete,
                # consistent source mapping, not per-value alias guessing.
                plain = all(k in observed_properties for k in wanted_properties)
                suffixed = all(k + '_1' in observed_properties for k in wanted_properties)
                if not plain:
                    assert not any(k in observed_properties for k in wanted_properties) and suffixed, 'incomplete overlay source schema'
                    observed_properties = {k: observed_properties[k + '_1'] for k in wanted_properties}
                elif any(k + '_1' in observed_properties for k in wanted_properties):
                    assert suffixed and all(observed_properties[k] == observed_properties[k + '_1'] for k in wanted_properties), 'conflicting overlay source schema'
            assert all(k in observed_properties and observed_properties[k] == v for k, v in wanted_properties.items()), 'geometry detached from its source record properties'
        assert candidate.geom_type == geometry.geom_type or {candidate.geom_type, geometry.geom_type} <= {'Polygon', 'MultiPolygon'}
        assert candidate.hausdorff_distance(geometry) <= tolerance, 'coordinates differ from independent oracle'
        if geometry.area:
            assert abs(candidate.area - geometry.area) <= max(.5, geometry.area * .002), 'area differs'


def assert_isolated_workers(worker_attempts, *, historical=False):
    assert worker_attempts, 'missing durable worker attempts'
    for attempt in worker_attempts:
        identity = json.loads(attempt['executor_identity'])
        pid = identity.get('pid')
        assert type(pid) is int and pid > 0, 'missing worker process identity'
        if historical:
            # Windows can reuse a terminated worker PID for a later offline
            # adjudicator. Its PID is not the original execution parent PID.
            assert isinstance(identity.get('create_time'), (int, float)) and identity['create_time'] > 0, 'missing historical process creation time'
        else:
            assert pid != os.getpid(), 'worker did not run in a child process'


def assert_live_provenance(snapshot, planner_attempts, worker_attempts, kind, provider_calls=(), *, family=None, historical=False):
    assert planner_attempts, 'no durable planner attempt'
    assert provider_calls and all(c.get('response_sha256') for c in provider_calls), 'no observed real provider response'
    assert all(a.get('provider') not in ('e2e-deterministic', 'fake', 'mock') for a in planner_attempts)
    if kind == 'ambiguity':
        assert snapshot['status'] == 'awaiting_input', 'missing business value was not clarified'
        assert any(a.get('error_code') == 'CLARIFICATION_REQUIRED' for a in planner_attempts), 'wrong reason for awaiting input'
        assert not snapshot.get('active_plan') and not worker_attempts and not snapshot.get('output_artifacts')
        return
    if kind == 'negative':
        assert family == 'bad_csv', 'negative scenario requires a declared exact diagnostic oracle'
        assert snapshot['status'] in ('failed', 'awaiting_input'), 'invalid input was silently accepted'
        assert not any(a.get('type') in ('Vector', 'Raster', 'Coordinate') for a in snapshot.get('output_artifacts', [])), 'invalid upload produced a spatial output'
        if snapshot['status'] == 'awaiting_input':
            assert not snapshot.get('active_plan') and not worker_attempts
            assert any(a.get('error_code') == 'CLARIFICATION_REQUIRED' for a in planner_attempts), 'wrong clarification diagnostic'
        else:
            assert snapshot.get('active_plan'), 'invalid coordinates must fail during coordinate validation'
            assert_isolated_workers(worker_attempts, historical=historical)
            assert any(a.get('tool_name') == 'io.csv_to_points' and a.get('status') == 'failed'
                and a.get('error_code') == 'COORDINATE_OUT_OF_RANGE' for a in worker_attempts), 'wrong failure stage or invalid-coordinate diagnostic'
        return
    assert snapshot['status'] == 'completed', f'run status {snapshot["status"]}'
    assert snapshot.get('active_plan') and snapshot.get('tasks')
    assert all(t['status'] == 'succeeded' for t in snapshot['tasks'])
    assert worker_attempts and any(a.get('status') == 'succeeded' for a in worker_attempts)
    assert_isolated_workers(worker_attempts, historical=historical)
    assert any(a.get('status') == 'succeeded' and a.get('response_hash') for a in planner_attempts)


def assert_upload_rejection(case, status, body):
    assert case.family == 'broken_vector', 'this scenario does not expect upload rejection'
    assert status == 422, 'malformed JSON must be rejected with HTTP 422'
    error = body.get('error', {})
    assert error.get('code') == 'ARTIFACT_INVALID', 'wrong upload error code'
    assert 'Expecting value:' in error.get('message', ''), 'upload rejected for a reason other than the malformed JSON fixture'


def _duration(start, end):
    try:
        return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000)
    except (TypeError, ValueError):
        return None


def advance_run(supervisor, client, run_id, worker_id):
    try:
        return supervisor.run_once(worker_id, run_id=run_id)
    except Exception:
        durable = client.get(f'/api/runs/{run_id}').json()
        if durable.get('status') not in ('failed', 'awaiting_input'):
            raise
        # A committed business rejection still needs the exact independent
        # negative oracle below; arbitrary failures never become success here.
        return None


def run_case(case, root, journal, budget, timeout=180):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore, RuntimeSupervisor
    from app.runtime.llm_planner import OpenAICompatiblePlanner

    class MeteredRealPlanner(OpenAICompatiblePlanner):
        def _call(self, messages):
            # Production may perform one compatibility retry even when normal
            # retries are disabled. Reserve both HTTP requests before dispatch.
            input_upper_bound = len(canonical(messages).encode('utf-8')) + len(canonical(self._response_format(messages)).encode('utf-8')) + 8192
            budget.reserve(case.case_id, calls=2, tokens=input_upper_bound + self.max_tokens)
            raw, elapsed = super()._call(messages)
            journal.append(dict(type='provider_response', case_id=case.case_id,
                model=self.model, provider=self.provider, latency_ms=elapsed,
                response_sha256=hashlib.sha256(canonical(raw).encode('utf-8')).hexdigest(),
                usage=raw.get('usage') if isinstance(raw.get('usage'), dict) else None))
            return raw, elapsed

    # Retrying is explicit; preserve every old database/artifact as evidence.
    prior_results = [r for r in journal.for_case(case.case_id) if r.get('type') == 'result']
    case_root = Path(root) / case.case_id / f'attempt-{len(prior_results) + 1:02d}'
    case_root.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeStore(RuntimeDB(case_root / 'runtime.db'))
    runtime.initialize()
    artifacts = ArtifactStore(case_root / 'data', runtime)
    planner = MeteredRealPlanner.from_settings()
    planner.max_retries = 0
    supervisor = RuntimeSupervisor(runtime, artifacts, planner=planner, isolate=True, autostart=False, timeout_seconds=timeout)
    # FastAPI/Pydantic builds response fields while an app is constructed.
    # Concurrent construction races over warnings.catch_warnings internals;
    # keep only this short setup step serial, not the real provider/workers.
    with _APP_CONSTRUCTION_LOCK:
        client = TestClient(create_app(runtime_store=runtime, artifact_store=artifacts, runtime_supervisor=supervisor))
    started = time.monotonic()
    stage = 'upload'
    stage_times = {}
    snapshot = {}
    run_id = None
    result = dict(type='result', suite='real_api_llm_worker_artifact', case_id=case.case_id,
        family=case.family, kind=case.kind, prompt=case.prompt, file_format=case.file_format,
        ok=False, stages={}, run_id=None, model=planner.model, provider=planner.provider)
    try:
        all_previous = journal.for_case(case.case_id)
        last_result = max((i for i, r in enumerate(all_previous) if r.get('type') == 'result'), default=-1)
        previous = all_previous[last_result + 1:]
        session_records = [r for r in previous if r.get('type') == 'session_created']
        if session_records:
            session_id = session_records[-1]['session_id']
        else:
            response = client.post('/api/sessions')
            assert response.status_code == 201, f'session API returned {response.status_code}'
            session_id = response.json()['id']
            journal.append(dict(type='session_created', case_id=case.case_id, session_id=session_id))
        upload_records = {r['filename']: r for r in previous if r.get('type') == 'uploaded'}
        ids = []
        for filename, content, crs in build_uploads(case, case_root / 'fixtures'):
            content_hash = hashlib.sha256(content).hexdigest()
            if filename in upload_records:
                record = upload_records[filename]
                # GPKG containers carry timestamps; trust the retained upload
                # bytes and durable content hash rather than re-uploading it.
                artifact_id = record['artifact_id']
            else:
                response = client.post('/api/artifacts/uploads', files={'file': (filename, content, 'application/octet-stream')},
                    data={'declared_crs': canonical(crs)})
                if case.family == 'broken_vector':
                    assert_upload_rejection(case, response.status_code, response.json())
                    assert runtime._read(lambda c: c.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0]) == 0
                    journal.append(dict(type='upload_rejected', case_id=case.case_id, sha256=content_hash,
                        byte_size=len(content), http_status=response.status_code, diagnostic=response.json()['error']['code']))
                    result.update(ok=True, status='upload_rejected', evidence_scope='upload_validation_only',
                        stages=dict(api=True, planner=False, compiled=False, worker=False, artifact=False, semantic=True))
                    stage_times['upload_ms'] = round((time.monotonic() - started) * 1000, 2)
                    return result
                assert response.status_code == 201, f'upload API {response.status_code}: {response.text[:300]}'
                artifact = response.json()
                artifact_id = artifact['artifact_id']
                assert artifact['content_hash'] == content_hash, 'upload content hash differs'
                check = client.get(f'/api/artifacts/{artifact_id}/content')
                assert check.status_code == 200 and check.content == content, 'upload download round-trip differs'
                journal.append(dict(type='uploaded', case_id=case.case_id, filename=filename, artifact_id=artifact_id,
                    sha256=content_hash, byte_size=len(content)))
            ids.append(artifact_id)
        stage_times['upload_ms'] = round((time.monotonic() - started) * 1000, 2)
        stage = 'api'
        response = client.post('/api/runs', json=request_payload(case, session_id, ids))
        assert response.status_code == 202, f'run API returned {response.status_code}: {response.text[:300]}'
        run_id = response.json()['run_id']
        result['run_id'] = run_id
        journal.append(dict(type='case_started', case_id=case.case_id, run_id=run_id, session_id=session_id))
        stage = 'execution'
        deadline = started + timeout
        while time.monotonic() < deadline:
            snapshot = client.get(f'/api/runs/{run_id}').json()
            if snapshot.get('status') in ('completed', 'failed', 'cancelled', 'awaiting_input'):
                break
            supervisor.queue.recover_expired_leases(run_id=run_id)
            advance_run(supervisor, client, run_id, f'live-{case.case_id}')
            time.sleep(.01)
        else:
            # Cancel through the same public boundary, so no child work keeps
            # spending resources after the timeout is reported.
            client.post(f'/api/runs/{run_id}/cancel', json={'command_id': f'timeout-{case.case_id}'})
            raise TimeoutError('case deadline exceeded')
        planner_attempts = runtime._read(lambda c: [dict(r) for r in c.execute(
            'SELECT * FROM planner_attempts WHERE run_id=? ORDER BY created_at', (run_id,))])
        worker_attempts = runtime._read(lambda c: [dict(r) for r in c.execute(
            'SELECT a.*,t.tool_name FROM task_attempts a JOIN tasks t ON t.task_id=a.task_id JOIN plans p ON p.plan_id=t.plan_id WHERE p.run_id=? ORDER BY a.started_at', (run_id,))])
        case_records = journal.for_case(case.case_id)
        last_result = max((i for i, r in enumerate(case_records) if r.get('type') == 'result'), default=-1)
        calls = [r for r in case_records[last_result + 1:] if r.get('type') == 'provider_response']
        result['planner_attempts'] = [{k: a.get(k) for k in ('attempt_id', 'phase', 'status', 'provider', 'model', 'response_hash', 'latency_ms', 'error_code')} for a in planner_attempts]
        result['worker_attempts'] = [{k: a.get(k) for k in ('attempt_id', 'task_id', 'tool_name', 'status', 'executor_identity', 'error_code')} for a in worker_attempts]
        result['status'] = snapshot['status']
        result['usage'] = [c.get('usage') for c in calls]
        result['repairs'] = sum(a.get('phase') == 'repair' for a in planner_attempts)
        stage_times['planner_ms'] = sum(c.get('latency_ms', 0) for c in calls)
        durations = [_duration(a.get('started_at'), a.get('finished_at')) for a in worker_attempts]
        stage_times['worker_ms'] = sum(d for d in durations if d is not None)
        result['stages'].update(api=True, planner=bool(calls), compiled=bool(snapshot.get('active_plan')),
            worker=bool(worker_attempts), artifact=False, semantic=False)
        stage = 'provenance'
        assert_live_provenance(snapshot, planner_attempts, worker_attempts, case.kind, calls, family=case.family)
        stage = 'artifact'
        outputs = []
        # Read the API snapshots/content path. Expected results never consult
        # planner task names; leaf outputs exclude intermediate imported data.
        leaves = runtime._read(lambda c: {r[0] for r in c.execute(
            'SELECT o.artifact_id FROM task_outputs o JOIN tasks t ON t.task_id=o.task_id JOIN plans p ON p.plan_id=t.plan_id WHERE p.run_id=? AND NOT EXISTS (SELECT 1 FROM task_inputs i JOIN tasks consumer ON consumer.task_id=i.task_id WHERE i.artifact_id=o.artifact_id AND consumer.plan_id=p.plan_id)', (run_id,))})
        for artifact in snapshot.get('output_artifacts', []):
            if artifact['artifact_id'] not in leaves:
                continue
            assert artifact['validation_status'] == 'valid'
            response = client.get(artifact['content_url'])
            assert response.status_code == 200
            assert hashlib.sha256(response.content).hexdigest() == artifact['content_hash']
            outputs.append(dict(artifact, payload=response.content))
        result['outputs'] = [{k: a.get(k) for k in ('artifact_id', 'type', 'crs', 'content_hash', 'byte_size')} for a in outputs]
        result['stages']['artifact'] = bool(outputs)
        stage = 'semantic'
        check_started = time.monotonic()
        if case.kind == 'success':
            assert_semantics(case, outputs)
        result['stages']['semantic'] = True
        stage_times['semantic_ms'] = round((time.monotonic() - check_started) * 1000, 2)
        result['ok'] = True
    except Exception as exc:
        result['failure_stage'] = stage
        result['failure_type'] = type(exc).__name__
        # Exceptions here contain API diagnostics, never provider headers/key.
        result['failure_detail'] = str(exc)[:1000]
        if planner.api_key:
            result['failure_detail'] = result['failure_detail'].replace(planner.api_key, '[REDACTED]')
        records = journal.for_case(case.case_id)
        last = max((i for i, r in enumerate(records) if r.get('type') == 'result'), default=-1)
        if any(r.get('type') == 'budget_denied' for r in records[last + 1:]):
            result['failure_type'] = 'BudgetExhausted'
        result['status'] = snapshot.get('status', 'not_started')
    finally:
        result['timings'] = dict(stage_times, total_ms=round((time.monotonic() - started) * 1000, 2))
        if run_id:
            event_rows = runtime.list_events(run_id)
            result['events'] = [{'seq': e['seq'], 'type': e['event_type'], 'created_at': e.get('created_at')} for e in event_rows]
        client.close()
        supervisor.stop(timeout=5)
        journal.append(result)
    return result


def summary(journal, selected):
    results = [journal.completed[c.case_id] for c in selected if c.case_id in journal.completed]
    positive = [r for r in results if r['kind'] == 'success']
    reserved = [r for r in journal.records if r.get('type') == 'call_reserved']
    calls = [r for r in journal.records if r.get('type') == 'provider_response']
    complete_usage = bool(calls) and all(isinstance(c.get('usage'), dict) and all(
        type(c['usage'].get(k)) is int and c['usage'][k] >= 0 for k in ('prompt_tokens', 'completion_tokens')) for c in calls)
    return dict(suite='real_api_llm_worker_artifact', corpus_sha256=CORPUS_SHA256, runner_version=RUNNER_VERSION, runner_sha256=runner_digest(),
        evidence_valid=not any(r.get('type') == 'runtime_drift' for r in journal.records),
        selected=len(selected), completed=len(results), passed=sum(r['ok'] for r in results),
        failed=sum(not r['ok'] for r in results), pending=len(selected)-len(results),
        positive_e2e_passed=sum(r['ok'] for r in positive), positive_total=len(positive),
        safe_negative_or_clarification_passed=sum(r['ok'] for r in results if r['kind'] != 'success' and r.get('status') != 'upload_rejected'),
        safe_upload_rejections=sum(r['ok'] and r.get('status') == 'upload_rejected' for r in results),
        stages={name: sum(bool(r.get('stages', {}).get(name)) for r in results) for name in ('api', 'planner', 'compiled', 'worker', 'artifact', 'semantic')},
        reserved_http_requests=sum(r['calls'] for r in reserved), reserved_tokens=sum(r['tokens'] for r in reserved),
        reserved_cost_usd=round(sum(r['estimated_cost_usd'] for r in reserved), 6),
        provider_response_count=len(calls), actual_usage_complete=complete_usage,
        actual_prompt_tokens=sum(c['usage']['prompt_tokens'] for c in calls) if complete_usage else None,
        actual_completion_tokens=sum(c['usage']['completion_tokens'] for c in calls) if complete_usage else None,
        reported_http_requests=sum(c.get('usage', {}).get('planner_http_calls', 0) for c in calls if isinstance(c.get('usage'), dict)),
        model_names=sorted({r.get('model', '') for r in results}))


def select_cases(cases, limit, seed, smoke_families=False, *, family=None):
    if family is not None:
        if smoke_families:
            raise ValueError('family filter cannot be combined with family smoke selection')
        if family not in {case.family for case in cases}:
            raise ValueError(f'unknown family: {family}')
    selected = list(cases)
    random.Random(seed).shuffle(selected)
    if family is not None:
        selected = [case for case in selected if case.family == family]
    if smoke_families:
        families = list(dict.fromkeys(c.family for c in cases))
        chosen = [next(c for c in selected if c.family == family and c.profile == index % 10)
            for index, family in enumerate(families)]
        return chosen[:limit]
    return selected[:limit]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly enable real paid provider calls')
    parser.add_argument('--inventory', action='store_true', help='Offline corpus summary only; never marks E2E passing')
    parser.add_argument('--export-corpus', type=Path, help='Write the expanded frozen 2000-case JSONL corpus')
    parser.add_argument('--output', type=Path, default=Path('.gismind/live-prompt-2000'))
    parser.add_argument('--limit', type=int, default=2000, help='Smaller smoke count; full release count is 2000')
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--smoke-families', action='store_true', help='Select one case per family with five upload formats, at most 20 cases')
    selection.add_argument('--family', help='Select only one frozen task family for a targeted live run')
    parser.add_argument('--retry-failed', action='store_true', help='Explicitly retry failed cases in fresh attempt directories; original evidence is retained')
    parser.add_argument('--seed', type=int, default=SEED, help='Deterministic execution shuffle, never changes prompts')
    parser.add_argument('--concurrency', type=int, choices=range(1, 5), default=1)
    parser.add_argument('--qps', type=float, default=.5, help='Average HTTP request admission rate; compatibility burst at most 2')
    parser.add_argument('--max-provider-requests', type=int, default=8000)
    parser.add_argument('--max-reserved-tokens', type=int, default=500_000_000)
    parser.add_argument('--max-cost-usd', type=float, help='Required conservative estimated spend ceiling for live execution')
    parser.add_argument('--usd-per-million', type=float, default=10, help='Operator supplied upper token price; not an invoice')
    parser.add_argument('--timeout', type=float, default=180)
    args = parser.parse_args(argv)
    cases = load_cases()
    if len(cases) != 2000 or corpus_digest(cases) != CORPUS_SHA256:
        parser.error('frozen corpus integrity check failed')
    if args.family is not None and args.family not in {case.family for case in cases}:
        parser.error(f'unknown family: {args.family}')
    if args.export_corpus:
        args.export_corpus.parent.mkdir(parents=True, exist_ok=True)
        args.export_corpus.write_text(''.join(canonical(asdict(c)) + '\n' for c in cases), encoding='utf-8')
    if args.inventory:
        from collections import Counter
        print(canonical(dict(suite='offline_inventory_not_live_e2e', count=len(cases), sha256=CORPUS_SHA256,
            kinds=dict(Counter(c.kind for c in cases)), families=dict(Counter(c.family for c in cases)),
            formats=dict(Counter(c.file_format for c in cases)))))
        return 0
    if not args.live:
        parser.error('pass --live to enable provider calls, or --inventory for offline inspection')
    if not 1 <= args.limit <= 2000 or not math.isfinite(args.qps) or not math.isfinite(args.timeout) or args.qps <= 0 or args.timeout <= 0:
        parser.error('limit must be 1..2000 and qps/timeout must be positive')
    if args.max_cost_usd is None or not math.isfinite(args.max_cost_usd) or not math.isfinite(args.usd_per_million) or min(args.max_cost_usd, args.usd_per_million, args.max_provider_requests, args.max_reserved_tokens) <= 0:
        parser.error('a positive --max-cost-usd and positive budget limits are required')
    from app.config import Settings, get_settings
    from app.runtime.llm_planner import PROMPT_VERSION
    settings = get_settings()
    try:
        require_provider({name: getattr(settings, name) for name in ('LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL')})
    except ValueError as exc:
        parser.error(str(exc))
    args.output.mkdir(parents=True, exist_ok=True)
    # An exclusive OS file lock is released even after an abrupt process exit.
    with (args.output / 'runner.lock').open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            parser.error('another runner owns this output directory')
        journal = Journal(args.output / 'results.jsonl', CORPUS_SHA256)
        source_root = Path(__file__).resolve().parents[2] / 'app'

        def current_runtime():
            # Re-read nonsecret settings each batch; never hash .env bytes,
            # provider keys, or any credential-bearing settings dictionary.
            return runtime_fingerprint(source_root, Settings(),
                prompt_version=PROMPT_VERSION, case_timeout=args.timeout)

        baseline_runtime = current_runtime()
        try:
            validate_runtime_manifest(journal, baseline_runtime)
        except ValueError as exc:
            parser.error(str(exc))
        model_manifest = [r for r in journal.records if r.get('type') == 'provider_manifest']
        identity = dict(model=settings.LLM_MODEL, endpoint_hash=hashlib.sha256(settings.LLM_BASE_URL.encode()).hexdigest())
        if model_manifest and any(model_manifest[0].get(k) != v for k, v in identity.items()):
            parser.error('resume requires the same provider/model; select a new output directory')
        if not model_manifest:
            journal.append(dict(type='provider_manifest', runtime=baseline_runtime, **identity))
        selected = select_cases(cases, args.limit, args.seed, args.smoke_families, family=args.family)
        budget = CallBudget(journal, max_calls=args.max_provider_requests, max_tokens=args.max_reserved_tokens,
            max_cost=args.max_cost_usd, reserve_tokens=0, usd_per_million=args.usd_per_million, min_interval=1 / args.qps)
        completed = journal.completed
        pending = [c for c in selected if c.case_id not in completed or (args.retry_failed and not completed[c.case_id]['ok'])]
        invocation_start = len(journal.records)
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            # Bounded batches prevent thousands of queued futures and admit
            # no more work once the reservation ceiling has been reached.
            for offset in range(0, len(pending), args.concurrency):
                if not check_runtime_batch(journal, baseline_runtime, current_runtime):
                    break
                if any(r.get('type') == 'budget_denied' for r in journal.records[invocation_start:]):
                    break
                futures = [pool.submit(run_case, c, args.output / 'cases', journal, budget, args.timeout)
                    for c in pending[offset:offset + args.concurrency]]
                for future in as_completed(futures):
                    outcome = future.result()
                    print(canonical(dict(case_id=outcome['case_id'], ok=outcome['ok'],
                        status=outcome.get('status'), failure_stage=outcome.get('failure_stage'),
                        completed=len(journal.completed), selected=len(selected))), flush=True)
                # Catch changes made while this batch was running as well;
                # all outcomes stay available, but the run cannot pass a gate.
                if not check_runtime_batch(journal, baseline_runtime, current_runtime):
                    break
        report = summary(journal, selected)
        report['runtime_fingerprint'] = baseline_runtime['fingerprint']
        report['full_2000_gate'] = report['evidence_valid'] and len(selected) == 2000 and report['completed'] == 2000 and report['failed'] == 0
        (args.output / 'summary.json').write_text(canonical(report) + '\n', encoding='utf-8')
        print(canonical(report))
        return 0 if report['evidence_valid'] and report['completed'] == len(selected) and not report['failed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
