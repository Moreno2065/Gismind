"""Physical fixtures and independent GIS/provenance oracles for the live browser gate."""
from __future__ import annotations

import hashlib
import json
import math
from numbers import Real
from pathlib import Path
import random


def _collection(features):
    return {'type': 'FeatureCollection', 'features': features}


def _point(x, y, properties):
    return {'type': 'Feature', 'properties': properties, 'geometry': {'type': 'Point', 'coordinates': [x, y]}}


HOUSING_PROMPT = (
    '请从房源附件中筛选总价万元不超过300且面积平方米不少于90的房源，'
    '同时必须位于第二个附件的学区范围多边形内。学区资格仅按给定范围做空间筛选，不是入学承诺。'
    '保留房源原始属性，在地图上展示最终房源，并导出最终筛选结果为GeoPackage（GPKG）供下载。'
)
CASES = {
    'buffer': {'prompt': '请对上传的站点生成500米缓冲区，保留站点属性，在地图展示最终缓冲区，并导出缓冲区为GeoPackage（GPKG）供下载。'},
    'multi_step': {'prompt': '请按顺序完成全部步骤：\n1、把上传站点中的site_id字段重命名为“站点编号”。\n2、为每个站点生成500米缓冲区。\n3、在地图上显示缓冲结果。\n4、导出同一缓冲结果为GeoPackage（GPKG）供下载。'},
    'housing': {'prompt': HOUSING_PROMPT},
    'housing_clarify': {'prompt': '帮我挑学区房，要便宜、上学方便。条件不明确的地方请先问我。', 'reply': '不考虑通学距离或时间，只按以下明确条件筛选。' + HOUSING_PROMPT},
}
BASELINE_CASES = tuple(CASES)
CASES.update({
    'sequential_buffers': {'prompt': '请按顺序完成：1、对原始站点做100米缓冲。2、对上一步缓冲结果再做400米缓冲。3、在地图显示最后结果并导出GPKG。保留原始站点属性。'},
    'housing_exclude': {'prompt': '请排除所有总价万元超过300的房源，价格缺失的房源不要因此删掉。对剩余结果筛选面积平方米至少90且必须位于第二个附件的学区范围多边形内。保留全部原属性，在地图显示最终房源，并导出同一结果为GPKG。仅作空间筛选，不承诺入学。', 'seeded': True, 'exclude_price': True},
    'housing_exclude_postfix': {'prompt': '把总价万元超过300的房源排除，价格缺失的房源不要因此删掉。对剩余结果筛选面积平方米至少90且必须位于第二个附件的学区范围多边形内。保留全部原属性，在地图显示最终房源，并导出同一结果为GPKG。仅作空间筛选，不承诺入学。', 'seeded': True, 'exclude_price': True},
    'housing_seeded': {'prompt': '总价上限是300万元（包含300），面积下限是90平方米（包含90）。请同时满足这两项，并只留下学区附件多边形内部的房源。保留房源全部原属性，显示最终房源地图，另将同一份结果导出为GPKG下载。学区范围只作空间筛选，不代表入学资格。', 'seeded': True},
    'housing_projected': {'prompt': '两个附件的原始坐标均为EPSG:3857。' + HOUSING_PROMPT, 'seeded': True, 'input_crs': 3857},
    'housing_planning_refresh': {'prompt': HOUSING_PROMPT, 'recovery': 'planning_refresh'},
    'housing_planning_reconnect': {'prompt': HOUSING_PROMPT, 'recovery': 'planning_reconnect'},
    'housing_awaiting_refresh': {**CASES['housing_clarify'], 'recovery': 'awaiting_refresh'},
})


def fixture_uploads(case_id, *, seed=0):
    case = CASES[case_id]
    if case_id in {'buffer', 'multi_step', 'sequential_buffers'}:
        return [('站点.geojson', _collection([_point(118.8, 32.05, {'site_id': 'S1'})]))]
    houses = [_point(118.800, 32.050, {'房源ID': 'A', '总价万元': 280, '面积平方米': 100}),
              _point(118.801, 32.051, {'房源ID': 'B', '总价万元': 350, '面积平方米': 110}),
              _point(118.802, 32.052, {'房源ID': 'C', '总价万元': 260, '面积平方米': 80}),
              _point(118.820, 32.070, {'房源ID': 'D', '总价万元': 280, '面积平方米': 100})]
    zone = {'type': 'Feature', 'properties': {'范围名称': '实验给定学区范围', '用途': '仅用于空间筛选，不承诺入学'},
            'geometry': {'type': 'Polygon', 'coordinates': [[[118.79, 32.04], [118.81, 32.04], [118.81, 32.06], [118.79, 32.06], [118.79, 32.04]]]}}
    if case.get('seeded'):
        rng = random.Random(seed)
        # Three positives, including exact attribute limits, plus independent
        # price/area/spatial negatives. No identity is a semantic selection cue.
        values = [(280, 100, False), (300, 90, False), (299.999, 90.001, False),
                  (300.001, 100, False), (280, 89.999, False), (280, 100, True)]
        if case.get('exclude_price'):
            values.append((None, 100, False))
        identifiers = rng.sample(range(10000, 99999), len(values))
        houses = [_point(118.82 if outside else 118.797 + rng.random() * .006,
                         32.07 if outside else 32.047 + rng.random() * .006,
                         {'房源ID': f'H{identifier}', '总价万元': price, '面积平方米': area,
                          '备注': None if index % 2 == 0 else '实测样本', '楼层': index + 1})
                  for index, ((price, area, outside), identifier) in enumerate(zip(values, identifiers))]
        rng.shuffle(houses)
    fixtures = [('房源.geojson', _collection(houses)), ('学区范围.geojson', _collection([zone]))]
    if case.get('input_crs') == 3857:
        from pyproj import Transformer
        from shapely.geometry import mapping, shape
        from shapely.ops import transform
        project = Transformer.from_crs(4326, 3857, always_xy=True).transform
        for _, collection in fixtures:
            collection['crs'] = {'type': 'name', 'properties': {'name': 'EPSG:3857'}}
            for feature in collection['features']:
                feature['geometry'] = mapping(transform(project, shape(feature['geometry'])))
    return fixtures


def expected_housing(case_id, *, seed=0):
    """Evaluate business predicates directly on fixtures, independently of Runtime."""
    import geopandas as gpd
    uploads = fixture_uploads(case_id, seed=seed)
    crs = CASES[case_id].get('input_crs', 4326)
    houses = gpd.GeoDataFrame.from_features(uploads[0][1]['features'], crs=crs).to_crs(4326)
    zones = gpd.GeoDataFrame.from_features(uploads[1][1]['features'], crs=crs).to_crs(4326)
    inside = houses.geometry.map(lambda point: any(point.within(zone) for zone in zones.geometry))
    price = ~(houses['总价万元'] > 300) if CASES[case_id].get('exclude_price') else houses['总价万元'] <= 300
    return houses[price & (houses['面积平方米'] >= 90) & inside]


def source_hashes():
    root = Path(__file__).resolve().parents[3]
    files = []
    for directory in ('backend/app', 'backend/tests/browser', 'backend/tests/live', 'frontend/src', 'frontend/scripts', '.github/workflows'):
        files.extend(p for p in (root / directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    for pattern in ('backend/requirements*', 'backend/constraints*', 'frontend/package*.json', 'frontend/*config*', 'backend/.env*', 'frontend/.env*'):
        files.extend(p for p in root.glob(pattern) if p.is_file())
    # Secrets themselves are never copied into evidence; only exact local-byte hashes.
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(files))}


def verify_download(case_id, path, *, seed=0):
    import geopandas as gpd
    from shapely.geometry import Point

    assert path.read_bytes()[:16] == b'SQLite format 3\x00', 'download is not physical GeoPackage bytes'
    frame = gpd.read_file(path)
    assert frame.crs is not None and not frame.empty, 'download has no CRS or features'
    geographic = frame.to_crs(4326)
    assert geographic.geometry.is_valid.all(), 'invalid downloaded geometry'
    if case_id in {'buffer', 'multi_step', 'sequential_buffers'}:
        if case_id == 'multi_step':
            assert 'site_id' not in frame.columns and '站点编号' in frame.columns, 'rename instruction was omitted'
        identity = '站点编号' if case_id == 'multi_step' else 'site_id'
        assert len(frame) == 1 and frame.iloc[0][identity] == 'S1', 'buffer lost source identity'
        assert geographic.geom_type.isin(['Polygon', 'MultiPolygon']).all(), 'buffer did not produce polygons'
        # Local azimuthal equidistant CRS measures radius from the source point independently.
        local = frame.to_crs('+proj=aeqd +lat_0=32.05 +lon_0=118.8 +datum=WGS84 +units=m')
        polygon = local.geometry.iloc[0]
        assert polygon.covers(Point(0, 0)), '500 m buffer excludes source point'
        assert math.isclose(polygon.area, math.pi * 500 ** 2, rel_tol=.025), '500 m buffer area differs from independent metric oracle'
        vertices = list(polygon.exterior.coords) if polygon.geom_type == 'Polygon' else [xy for piece in polygon.geoms for xy in piece.exterior.coords]
        assert all(abs(math.hypot(x, y) - 500) <= 8 for x, y in vertices), '500 m buffer radius differs from independent metric oracle'
    else:
        expected = expected_housing(case_id, seed=seed)
        assert '房源ID' in frame and sorted(frame['房源ID']) == sorted(expected['房源ID']), 'housing IDs violate AND attributes or school-zone geometry'
        _verify_feature_multiset(expected, geographic, required_properties=set(expected.columns) - {'geometry'}, label='housing')
    return {'reader': 'geopandas/GDAL', 'crs': str(frame.crs), 'feature_count': len(frame),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size,
            'oracle': 'renamed_identity_and_metric_500m_polygon' if case_id == 'multi_step' else 'metric_500m_polygon' if case_id in {'buffer', 'sequential_buffers'} else 'fixture_derived_AND_price_area_school_polygon'}


def _same_property(left, right):
    import pandas as pd
    if pd.isna(left) or pd.isna(right):
        return bool(pd.isna(left) and pd.isna(right))
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, Real) and isinstance(right, Real):
        return bool(left == right or math.isclose(left, right, rel_tol=0, abs_tol=1e-9))
    return type(left) is type(right) and left == right


def _verify_feature_multiset(left, right, *, required_properties, label):
    assert len(left) == len(right), f'{label} feature counts differ'
    assert required_properties.issubset(left.columns) and required_properties.issubset(right.columns), f'{label} required properties are missing'
    # Bipartite matching consumes each row once, including coincident features.
    # Augmenting paths also avoid a greedy false negative near the tolerance.
    candidates = [[j for j, (_, other) in enumerate(right.iterrows())
                   if row.geometry.equals_exact(other.geometry, tolerance=1e-7, normalize=True)
                   and all(_same_property(row[key], other[key]) for key in required_properties)]
                  for _, row in left.iterrows()]
    matched = {}

    def assign(index, seen):
        for target in candidates[index]:
            if target in seen:
                continue
            seen.add(target)
            if target not in matched or assign(matched[target], seen):
                matched[target] = index
                return True
        return False

    assert all(assign(index, set()) for index in range(len(candidates))), f'{label} geometry/properties differ (one-to-one multiset)'


def verify_map_matches_download(content, downloaded, *, required_properties=None):
    import geopandas as gpd
    features = [feature for layer in content.get('layers', []) if layer.get('type') == 'FeatureCollection' for feature in layer.get('features', [])]
    assert features, 'MapLayer has no actual GeoJSON geometry'
    crs = content.get('crs', {})
    assert crs.get('kind') in {'EPSG', 'WGS84'}, 'map oracle requires declared source CRS'
    projected = gpd.GeoDataFrame.from_features(features, crs=f"EPSG:{crs.get('code', 4326)}").to_crs(4326)
    exported = gpd.read_file(downloaded).to_crs(4326)
    required = set(required_properties) if required_properties is not None else (set(projected.columns) | set(exported.columns)) - {'geometry'}
    assert all(required.issubset(feature.get('properties') or {}) for feature in features), 'map and final download required properties are missing'
    _verify_feature_multiset(projected, exported, required_properties=required, label='map and final download')


def verify_provenance(attempts, workers, provider, *, server_pid):
    observed = {row['response_hash'] for row in provider.get('responses', []) if row.get('http_calls', 0) >= 1}
    assert observed and attempts, 'no observed provider responses or durable attempts'
    assert all(row['provider'] == 'openai-compatible' and row['model'] == 'deepseek-flash' for row in attempts), 'wrong real provider/model'
    assert all(row.get('response_hash') in observed for row in attempts if row.get('status') == 'succeeded'), 'durable planner attempt is not matched to observed provider response'
    assert any(row.get('status') == 'succeeded' and row.get('response_hash') in observed for row in attempts), 'no succeeded observed provider attempt'
    assert workers and all(row.get('attempt_status') == 'succeeded' for row in workers), 'worker task did not succeed'
    for row in workers:
        identity = json.loads(row['executor_identity'])
        assert isinstance(identity.get('pid'), int) and identity['pid'] != server_pid, 'GIS task did not run in isolated worker process'
