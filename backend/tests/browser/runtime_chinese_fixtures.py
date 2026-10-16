"""Fresh physical uploads and independent GIS oracles for the Chinese browser corpus.

No Runtime handler, planner response or recorded result supplies expected data.
The browser owns uploading these bytes and downloading authoritative final artifacts.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import io
import json
import math
from pathlib import Path
import random
import tempfile
import zipfile
from xml.etree import ElementTree as ET


@dataclass(frozen=True)
class UploadFixture:
    filename: str
    mime_type: str
    content_bytes: bytes
    physical_format: str
    semantic_source: dict
    declaration: dict | None = None


_VECTOR_FORMATS = {'geojson': ('geojson', 'application/geo+json'),
                   'gpkg': ('gpkg', 'application/geopackage+sqlite3'),
                   'zip': ('shapefile_zip', 'application/zip'),
                   'kml': ('kml', 'application/vnd.google-earth.kml+xml'),
                   'csv': ('csv', 'text/csv'), 'tsv': ('tsv', 'text/tab-separated-values')}
_BUFFER_FAMILIES = {'buffer', 'sequential_buffers', 'rename_buffer', 'buffer_ambiguous'}
_HOUSING_FAMILIES = {'housing_multi', 'housing_exclude', 'housing_exclude_postfix', 'housing_ambiguous'}


def _collection(features, epsg, role):
    return {'type': 'FeatureCollection', 'features': features, 'epsg': epsg, 'role': role}


def _point(x, y, properties):
    return {'type': 'Feature', 'properties': properties,
            'geometry': {'type': 'Point', 'coordinates': [x, y]}}


def _frame(source):
    import geopandas as gpd
    return gpd.GeoDataFrame.from_features(source['features'], crs=source['epsg'])


def _project(source, epsg):
    if source['epsg'] == epsg:
        return source
    frame = _frame(source).to_crs(epsg)
    return _collection(json.loads(frame.to_json())['features'], epsg, source['role'])


def semantic_sources(case):
    """Seeded input facts, including exact thresholds and independent negatives."""
    params = case.params
    rng = random.Random(params['seed'])
    lon, lat = params.get('origin_lon', 118.8), params.get('origin_lat', 32.05)
    if case.family == 'raster_calculate':
        import numpy as np
        from rasterio.transform import from_origin
        from pyproj import Transformer
        size = 5 + int(getattr(case, 'profile', 0)) % 5
        values = np.array([rng.uniform(-5, 20) for _ in range(size * size)], dtype='float32').reshape(size, size)
        values[0, 0] = values[-1, -1] = -9999
        x, y = Transformer.from_crs(4326, params['source_epsg'], always_xy=True).transform(lon, lat)
        return [{'role': 'raster', 'epsg': params['source_epsg'], 'values': values.tolist(),
                 'transform': list(from_origin(x, y, 30, 30))[:6], 'nodata': -9999, 'dtype': 'float32'}]
    if case.family in _HOUSING_FAMILIES:
        price, area = params['price_max'], params['area_min']
        values = [(price - 20, area + 10, 'inside'), (price, area, 'inside'),
                  (price - .001, area + .001, 'inside'), (price + .001, area + 10, 'inside'),
                  (price - 20, area - .001, 'inside'), (price - 20, area + 10, 'outside'),
                  (price - 20, area + 10, 'boundary'), (None, area + 10, 'inside')]
        identifiers = rng.sample(range(10000, 99999), len(values))
        houses = []
        for index, ((cost, size, position), identity) in enumerate(zip(values, identifiers)):
            x = lon + .025 if position == 'outside' else lon + .01 if position == 'boundary' else lon + rng.uniform(-.005, .005)
            y = lat if position != 'inside' else lat + rng.uniform(-.005, .005)
            houses.append(_point(x, y, {'房号': f'房{identity}', '价格': cost, '面积': size,
                                       '备注': None if index % 2 == 0 else '中文样本'}))
        rng.shuffle(houses)
        zone = {'type': 'Feature', 'properties': {'名称': '给定范围'}, 'geometry': {
            'type': 'Polygon', 'coordinates': [[[lon - .01, lat - .01], [lon + .01, lat - .01],
                [lon + .01, lat + .01], [lon - .01, lat + .01], [lon - .01, lat - .01]]]}}
        return [_project(_collection(houses, 4326, 'houses'), params['source_epsg']),
                _project(_collection([zone], 4326, 'zone'), params['source_epsg'])]
    if case.family not in _BUFFER_FAMILIES | {'vector_reproject_filter'}:
        raise ValueError(f'unsupported case family: {case.family}')
    threshold = params['threshold']
    values = [threshold - 1, threshold, threshold + .001, threshold + 1, threshold + 5, threshold - .001]
    identifiers = rng.sample(range(10000, 99999), len(values))
    points = [_point(lon + index * .012 + rng.uniform(-.001, .001), lat + rng.uniform(-.003, .003),
        {'编号': identity, '名称': f'设施{identity}', '数值': value, '类别': '甲' if index % 2 else '乙'})
        for index, (value, identity) in enumerate(zip(values, identifiers))]
    rng.shuffle(points)
    return [_project(_collection(points, 4326, 'points'), params['source_epsg'])]


def _write_geojson(source, root):
    value = {'type': 'FeatureCollection', 'features': source['features'],
             'crs': {'type': 'name', 'properties': {'name': f"EPSG:{source['epsg']}"}}}
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')


def _write_gpkg(source, root):
    with tempfile.TemporaryDirectory(prefix='fixture-gpkg-', dir=root) as folder:
        path = Path(folder) / 'data.gpkg'
        _frame(source).to_file(path, layer='数据', driver='GPKG', engine='pyogrio')
        return path.read_bytes()


def _write_zip(source, root):
    assert all(len(key.encode('utf-8')) <= 10 for feature in source['features'] for key in feature['properties']), 'SHP field would truncate'
    with tempfile.TemporaryDirectory(prefix='fixture-shp-', dir=root) as folder:
        _frame(source).to_file(Path(folder) / 'data.shp', driver='ESRI Shapefile', encoding='UTF-8', engine='pyogrio')
        memory = io.BytesIO()
        with zipfile.ZipFile(memory, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(Path(folder).iterdir()):
                # Fixed ZIP timestamps make bytes reproducible for a given seed.
                member = zipfile.ZipInfo('图层/' + path.name.upper(), date_time=(2020, 1, 1, 0, 0, 0))
                member.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(member, path.read_bytes())
        return memory.getvalue()


def _write_delimited(source, root, delimiter):
    assert source['epsg'] == 4326, 'delimited fixtures require declared geographic coordinates'
    assert all(feature['geometry']['type'] == 'Point' for feature in source['features'])
    stream = io.StringIO(newline='')
    fields = list(source['features'][0]['properties'])
    writer = csv.writer(stream, delimiter=delimiter, lineterminator='\n')
    writer.writerow(['经度', '纬度', *fields])
    for feature in source['features']:
        writer.writerow([*feature['geometry']['coordinates'][:2], *(feature['properties'][field] for field in fields)])
    return stream.getvalue().encode('utf-8-sig')


def _write_csv(source, root):
    return _write_delimited(source, root, ',')


def _write_tsv(source, root):
    return _write_delimited(source, root, '\t')


def _write_kml(source, root):
    assert source['epsg'] == 4326, 'KML coordinates must be geographic'
    document_root = ET.Element('kml', xmlns='http://www.opengis.net/kml/2.2')
    document = ET.SubElement(document_root, 'Document')
    schema = ET.SubElement(document, 'Schema', name='fields', id='fields')
    fields = list(source['features'][0]['properties'])
    for field in fields:
        values = [feature['properties'][field] for feature in source['features'] if feature['properties'][field] is not None]
        kind = 'int' if all(isinstance(value, int) for value in values) else 'double' if all(isinstance(value, (int, float)) for value in values) else 'string'
        ET.SubElement(schema, 'SimpleField', name=field, type=kind)
    for feature in source['features']:
        assert feature['geometry']['type'] == 'Point', 'KML fixture writer supports points only'
        mark = ET.SubElement(document, 'Placemark')
        attributes = ET.SubElement(ET.SubElement(mark, 'ExtendedData'), 'SchemaData', schemaUrl='#fields')
        for field in fields:
            value = feature['properties'][field]
            if value is not None:
                ET.SubElement(attributes, 'SimpleData', name=field).text = str(value)
        point = ET.SubElement(mark, 'Point')
        ET.SubElement(point, 'coordinates').text = ','.join(str(value) for value in feature['geometry']['coordinates'][:2])
    return ET.tostring(document_root, encoding='utf-8', xml_declaration=True)


def _write_tif(source, root):
    import numpy as np
    from affine import Affine
    from rasterio.io import MemoryFile
    values = np.array(source['values'], dtype=source['dtype'])
    with MemoryFile() as memory:
        with memory.open(driver='GTiff', width=values.shape[1], height=values.shape[0], count=1,
                         dtype=source['dtype'], crs=f"EPSG:{source['epsg']}",
                         transform=Affine(*source['transform']), nodata=source['nodata']) as dataset:
            dataset.write(values, 1)
        return memory.read()


WRITERS = {'geojson': _write_geojson, 'gpkg': _write_gpkg, 'zip': _write_zip,
           'kml': _write_kml, 'csv': _write_csv, 'tsv': _write_tsv, 'tif': _write_tif}


def build_uploads(case, root):
    """Produce physical bytes before the browser uploads anything."""
    format_name = case.upload_format
    if format_name not in WRITERS:
        raise ValueError(f'unsupported upload format: {format_name}')
    raster = case.family == 'raster_calculate'
    if raster != (format_name == 'tif'):
        raise ValueError(f'unsupported upload format {format_name} for family {case.family}')
    if case.family in _HOUSING_FAMILIES | {'vector_reproject_filter'} and format_name not in {'geojson', 'gpkg', 'zip'}:
        raise ValueError(f'unsupported upload format {format_name} for family {case.family}')
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    uploads = []
    for source in semantic_sources(case):
        current_format = 'geojson' if source['role'] == 'zone' else format_name
        physical, mime = ('geotiff', 'image/tiff') if current_format == 'tif' else _VECTOR_FORMATS[current_format]
        stem = {'points': '站点', 'houses': '房源', 'zone': '学区范围', 'raster': '原始栅格'}[source['role']]
        uploads.append(UploadFixture(f'{stem}.{current_format}', mime,
            WRITERS[current_format](source, root), physical, source))
    return uploads


def expected_vector(case, uploads):
    """Business predicates applied directly to the generated source geometry."""
    source = _frame(uploads[0].semantic_source)
    if case.family in _HOUSING_FAMILIES:
        zones = _frame(uploads[1].semantic_source).to_crs(source.crs)
        inside = source.geometry.map(lambda point: any(point.within(zone) for zone in zones.geometry))
        price = source['价格'] <= case.params['price_max']
        if case.family in {'housing_exclude', 'housing_exclude_postfix'}:
            price = ~(source['价格'] > case.params['price_max'])
        return source[price & (source['面积'] >= case.params['area_min']) & inside].copy()
    selected = source[source['数值'] > case.params['threshold']].copy()
    if case.family == 'rename_buffer':
        selected = selected.rename(columns={'名称': '新名'})
    return selected


def _verify_buffer(case, expected, actual):
    from shapely.geometry import Point
    from tests.browser.runtime_live_browser_support import _same_property
    assert len(actual) == len(expected), 'buffer count violates source filter'
    assert sorted(actual['编号']) == sorted(expected['编号']), 'buffer identities violate source filter'
    required = set(expected.columns) - {'geometry'}
    assert required <= set(actual.columns), 'buffer source attributes missing'
    if case.family == 'rename_buffer':
        assert '名称' not in actual.columns and '新名' in actual.columns, 'rename instruction omitted'
    radius = (case.params['first_distance_m'] + case.params['second_distance_m']
              if case.family == 'sequential_buffers' else case.params['distance_m'])
    for _, row in expected.to_crs(4326).iterrows():
        result = actual[actual['编号'] == row['编号']]
        assert len(result) == 1, 'buffer contains duplicated identity'
        assert all(_same_property(row[key], result.iloc[0][key]) for key in required), 'buffer source attributes changed'
        center = row.geometry
        projected = result.to_crs(f'+proj=aeqd +lat_0={center.y} +lon_0={center.x} +datum=WGS84 +units=m')
        geometry = projected.geometry.iloc[0]
        assert geometry.geom_type == 'Polygon' and geometry.is_valid, 'buffer is not a valid polygon'
        assert not geometry.interiors and geometry.covers(Point(0, 0)), 'buffer has holes or excludes source'
        assert math.isclose(geometry.area, math.pi * radius ** 2, rel_tol=.025), 'buffer metric area differs'
        assert all(abs(math.hypot(x, y) - radius) <= max(8, radius * .02) for x, y, *_ in geometry.exterior.coords), 'buffer metric radius differs'


def verify_step_requirements(case, snapshot):
    """Use persisted task dependencies to distinguish ordered instructions."""
    if case.family not in _BUFFER_FAMILIES | {'vector_reproject_filter'}:
        return []
    tasks = {task['task_id']: task for task in snapshot['tasks']}
    def ancestors(task_id, seen):
        if task_id in seen:
            return seen
        assert task_id in tasks, 'task dependency is absent from snapshot'
        seen.add(task_id)
        for dependency in tasks[task_id].get('details', {}).get('dependencies', []):
            ancestors(dependency['task_id'], seen)
        return seen
    evidence = []
    filters = {task_id for task_id, task in tasks.items() if task.get('op') == 'attribute.filter'}
    if case.family == 'vector_reproject_filter':
        reprojections = [task for task in tasks.values() if task.get('op') == 'vector.reproject'
            and task.get('details', {}).get('parameters', {}).get('target_crs') == {'kind': 'EPSG', 'code': case.params['target_epsg']}]
        assert reprojections, 'requested vector reprojection operation is missing'
        selected = [task for task in reprojections if filters & ancestors(task['task_id'], set())]
        assert selected, 'source filter must precede requested reprojection'
        evidence.extend({'task_id': task['task_id'], 'op': task['op'], 'filter_ancestors': sorted(filters & ancestors(task['task_id'], set()))} for task in selected)
        return evidence
    buffers = [task for task in tasks.values() if task.get('op') == 'vector.buffer']
    assert buffers, 'buffer operation is missing'
    for task in buffers:
        upstream = ancestors(task['task_id'], set()) - {task['task_id']}
        assert filters & upstream, 'source filter must precede buffer'
        evidence.append({'task_id': task['task_id'], 'op': task['op'],
                         'parameters': task.get('details', {}).get('parameters', {}),
                         'filter_ancestors': sorted(filters & upstream), 'ancestor_task_ids': sorted(upstream)})
        if case.family == 'rename_buffer':
            renames = [tasks[identity] for identity in upstream if tasks[identity].get('op') == 'attribute.rename_field'
                and tasks[identity].get('details', {}).get('parameters', {}) == {'old_name': '名称', 'new_name': '新名'}]
            assert renames, 'requested rename must precede buffer'
            assert any(filters & ancestors(rename['task_id'], set()) for rename in renames), 'source filter must precede rename'
            evidence[-1]['rename_ancestors'] = sorted(rename['task_id'] for rename in renames)
    if case.family == 'sequential_buffers':
        assert len(buffers) == 2, 'two sequential buffer operations were not executed'
        first = [task for task in buffers if task.get('details', {}).get('parameters', {}).get('distance_m') == case.params['first_distance_m']]
        second = [task for task in buffers if task.get('details', {}).get('parameters', {}).get('distance_m') == case.params['second_distance_m']]
        assert len(first) == len(second) == 1, 'sequential buffer distances differ'
        assert first[0]['task_id'] in ancestors(second[0]['task_id'], set()), 'second buffer does not depend on first'
    return evidence


def _verify_raster(case, upload, path):
    import numpy as np
    import rasterio
    from affine import Affine
    source = upload.semantic_source
    values = np.asarray(source['values'], dtype=source['dtype'])
    valid = values != source['nodata']
    expected = values * case.params['factor'] + case.params['offset']
    with rasterio.open(path) as actual:
        assert actual.driver == 'GTiff' and actual.count == 1, 'final artifact is not a one-band GeoTIFF'
        assert actual.crs is not None and actual.crs.to_epsg() == source['epsg'], 'raster CRS changed'
        assert actual.nodata == source['nodata'], 'raster nodata definition changed'
        assert actual.transform == Affine(*source['transform']), 'raster grid changed'
        assert actual.shape == values.shape, 'raster dimensions changed'
        np.testing.assert_array_equal(actual.read_masks(1) > 0, valid, err_msg='raster missing-data mask changed')
        np.testing.assert_allclose(actual.read(1)[valid], expected[valid], rtol=1e-6, atol=1e-5,
                                   err_msg='raster arithmetic differs from independent array')
    return {'oracle': 'source_array_multiply_then_add', 'valid_pixels': int(valid.sum()),
            'width': values.shape[1], 'height': values.shape[0], 'crs': source['epsg']}


def verify_case(case, uploads, snapshot, downloaded_artifacts, case_root):
    """Validate actual final downloads; records are {'artifact': metadata, 'path': Path}."""
    assert snapshot['status'] == 'completed', 'Run did not complete'
    step_evidence = verify_step_requirements(case, snapshot)
    final_ids = set(snapshot.get('final_artifact_ids', []))
    assert final_ids, 'authoritative final artifact IDs missing'
    metadata = {item['artifact_id']: item for item in snapshot['output_artifacts'] if item['artifact_id'] in final_ids}
    assert set(metadata) == final_ids, 'final artifact metadata missing'
    expected_types = ['Raster'] if case.family == 'raster_calculate' else ['File', 'MapLayer']
    assert sorted(item['type'] for item in metadata.values()) == expected_types, 'final artifact types or counts differ from requested deliveries'
    downloads = {}
    for record in downloaded_artifacts:
        artifact = record.get('artifact', record)
        identity = artifact['artifact_id']
        assert identity in final_ids and identity in metadata, 'download is not an authoritative final artifact'
        assert identity not in downloads, 'final artifact downloaded more than once'
        path = Path(record['path'])
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == metadata[identity]['content_hash'], 'download differs from authoritative CAS hash'
        downloads[identity] = (metadata[identity], path)
    assert set(downloads) == final_ids, 'not every authoritative final artifact was downloaded'
    if case.family == 'raster_calculate':
        rasters = [(item, path) for item, path in downloads.values() if item['type'] == 'Raster']
        assert len(rasters) == 1, 'final Raster download missing or ambiguous'
        report = _verify_raster(case, uploads[0], rasters[0][1])
    else:
        import geopandas as gpd
        import pyogrio
        from tests.browser.runtime_live_browser_support import _verify_feature_multiset, verify_map_matches_download
        files = [(item, path) for item, path in downloads.values() if item['type'] == 'File']
        maps = [(item, path) for item, path in downloads.values() if item['type'] == 'MapLayer']
        assert len(files) == len(maps) == 1, 'final GPKG and map downloads missing or ambiguous'
        path = files[0][1]
        assert path.read_bytes()[:16] == b'SQLite format 3\x00', 'final file is not physical GeoPackage'
        assert pyogrio.read_info(path)['driver'] == 'GPKG', 'final SQLite file is not GeoPackage'
        actual = gpd.read_file(path)
        assert actual.crs is not None and not actual.empty and actual.geometry.is_valid.all(), 'invalid final spatial result'
        expected = expected_vector(case, uploads)
        required = set(expected.columns) - {'geometry'}
        if case.family in _BUFFER_FAMILIES:
            _verify_buffer(case, expected, actual)
        else:
            _verify_feature_multiset(expected.to_crs(4326), actual.to_crs(4326), required_properties=required, label=case.family)
        if case.family == 'vector_reproject_filter':
            assert actual.crs.to_epsg() == case.params['target_epsg'], 'requested target CRS was not exported'
        verify_map_matches_download(json.loads(maps[0][1].read_text(encoding='utf-8')), path, required_properties=required)
        report = {'oracle': 'fixture_predicates_and_metric_buffer' if case.family in _BUFFER_FAMILIES else 'fixture_predicates_and_geometry',
                  'feature_count': len(actual), 'required_properties': sorted(required), 'crs': str(actual.crs),
                  'expected_ids': sorted(expected['房号' if case.family in _HOUSING_FAMILIES else '编号'].tolist())}
    return {**report, 'family': case.family, 'input_formats': [upload.physical_format for upload in uploads],
            'input_sha256': [hashlib.sha256(upload.content_bytes).hexdigest() for upload in uploads],
            'verified_artifact_ids': sorted(downloads), 'step_evidence': step_evidence, 'case_root': str(case_root)}
