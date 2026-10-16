"""Offline physical fixture and result-oracle checks; no model calls."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def case(family='buffer', upload_format='geojson', **params):
    values = dict(seed=20261014, distance_m=100,
        first_distance_m=25, second_distance_m=75, threshold=10, price_max=200,
        area_min=60, factor=2, offset=1, source_epsg=3857 if family in {
            'vector_reproject_filter', 'raster_calculate'} else 4326,
        target_epsg=4326)
    values.update(params)
    return SimpleNamespace(case_id='offline-' + family, family=family, profile=0,
                           upload_format=upload_format, params=values)


@pytest.mark.parametrize('format_name', ['xlsx', 'kml', 'csv', 'tif'])
def test_old_upload_factory_rejects_unknown_format_before_writing(tmp_path, format_name):
    from tests.live.prompt_e2e_2000 import build_uploads
    invalid = SimpleNamespace(file_format=format_name, family='buffer', profile=0, count=4)
    with pytest.raises(ValueError, match='unsupported.*format'):
        build_uploads(invalid, tmp_path / 'must-not-create')
    assert not (tmp_path / 'must-not-create').exists()


def test_raster_gcj02_declaration_is_rejected_before_publication():
    from app.runtime.upload_inspection import validate_declarations
    facts = {'artifact_type': 'Raster', 'format': 'geotiff', 'crs': None}
    with pytest.raises(ValueError, match='栅格不支持 GCJ02'):
        validate_declarations(facts, {'kind': 'GCJ02'}, None)


@pytest.mark.parametrize('format_name,physical', [
    ('geojson', 'geojson'), ('gpkg', 'gpkg'), ('zip', 'shapefile_zip'),
    ('kml', 'kml'), ('csv', 'csv'), ('tsv', 'tsv'),
])
def test_real_writers_preserve_short_chinese_fields(tmp_path, format_name, physical):
    from tests.browser.runtime_chinese_fixtures import build_uploads
    from app.runtime.upload_inspection import inspect_file, read_table, read_vector
    upload = build_uploads(case(upload_format=format_name), tmp_path)[0]
    source = tmp_path / 'extensionless'
    source.write_bytes(upload.content_bytes)
    facts = inspect_file(source, 'misleading.xlsx')
    assert facts['format'] == physical == upload.physical_format
    required = {'编号', '名称', '数值', '类别'}
    assert required <= facts['fields'].keys()
    if physical in {'csv', 'tsv'}:
        actual = read_table(source, facts)['rows']
    else:
        actual = read_vector(source, facts).drop(columns='geometry').to_dict('records')
    expected = [feature['properties'] for feature in upload.semantic_source['features']]
    assert [{key: row[key] for key in sorted(required)} for row in sorted(actual, key=lambda row: row['编号'])] == [
        {key: row[key] for key in sorted(required)} for row in sorted(expected, key=lambda row: row['编号'])]


def test_new_factory_rejects_unknown_formats(tmp_path):
    from tests.browser.runtime_chinese_fixtures import build_uploads
    with pytest.raises(ValueError, match='unsupported.*format'):
        build_uploads(case(upload_format='xlsx'), tmp_path)


def test_raster_fixture_is_real_multivalue_geotiff(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile
    from tests.browser.runtime_chinese_fixtures import build_uploads
    upload = build_uploads(case('raster_calculate', 'tif'), tmp_path)[0]
    with MemoryFile(upload.content_bytes) as memory, memory.open() as actual:
        np.testing.assert_array_equal(actual.read(1), upload.semantic_source['values'])
        assert actual.crs.to_epsg() == 3857
        assert actual.nodata == -9999
        assert np.unique(actual.read(1)).size > 8


def test_housing_fixture_has_independent_boundary_and_missing_price_cases(tmp_path):
    from tests.browser.runtime_chinese_fixtures import build_uploads, expected_vector
    include_case = case('housing_multi')
    exclude_case = case('housing_exclude')
    included = expected_vector(include_case, build_uploads(include_case, tmp_path / 'include'))
    excluded = expected_vector(exclude_case, build_uploads(exclude_case, tmp_path / 'exclude'))
    assert len(included) == 3 and len(excluded) == 4
    assert included['价格'].max() == 200 and included['面积'].min() == 60
    assert excluded['价格'].isna().sum() == 1


def test_fixture_seed_changes_actual_bytes_and_ids(tmp_path):
    from tests.browser.runtime_chinese_fixtures import build_uploads
    first = case()
    second = case()
    second.params['seed'] += 1
    left = build_uploads(first, tmp_path / 'one')[0]
    right = build_uploads(second, tmp_path / 'two')[0]
    assert left.content_bytes != right.content_bytes
    assert left.semantic_source['features'] != right.semantic_source['features']


def final_downloads(tmp_path, frame=None, raster=None, map_frame=None):
    records = []
    if frame is not None:
        vector_path = tmp_path / 'final.gpkg'
        frame.to_file(vector_path, layer='结果', driver='GPKG')
        map_path = tmp_path / 'map.json'
        mapped = frame if map_frame is None else map_frame
        map_path.write_text(json.dumps({'crs': {'kind': 'EPSG', 'code': mapped.crs.to_epsg()},
            'layers': [json.loads(mapped.to_json())]}, ensure_ascii=False), encoding='utf-8')
        paths = [('art_file', 'File', vector_path), ('art_map', 'MapLayer', map_path)]
    else:
        paths = [('art_raster', 'Raster', raster)]
    for identity, kind, path in paths:
        artifact = {'artifact_id': identity, 'type': kind,
                    'content_hash': hashlib.sha256(path.read_bytes()).hexdigest()}
        records.append({'artifact': artifact, 'path': path})
    snapshot = {'status': 'completed', 'tasks': [
        {'task_id': 'filter', 'op': 'attribute.filter', 'details': {'dependencies': []}},
        {'task_id': 'project', 'op': 'vector.reproject', 'details': {'parameters': {'target_crs': {'kind': 'EPSG', 'code': 4326}}, 'dependencies': [{'task_id': 'filter'}]}},
    ],
                'output_artifacts': [record['artifact'] for record in records],
                'final_artifact_ids': [record['artifact']['artifact_id'] for record in records]}
    return snapshot, records


@pytest.mark.parametrize('family', ['housing_multi', 'housing_exclude', 'housing_exclude_postfix',
                                  'housing_ambiguous', 'vector_reproject_filter'])
def test_vector_oracle_accepts_real_files_and_rejects_changed_attributes(tmp_path, family):
    from tests.browser.runtime_chinese_fixtures import build_uploads, expected_vector, verify_case
    current = case(family)
    uploads = build_uploads(current, tmp_path / 'uploads')
    expected = expected_vector(current, uploads).to_crs(4326)
    valid = tmp_path / 'valid'
    valid.mkdir()
    snapshot, files = final_downloads(valid, frame=expected)
    report = verify_case(current, uploads, snapshot, files, valid)
    assert report['feature_count'] == len(expected)
    changed = expected.copy()
    field = '面积' if family.startswith('housing') else '数值'
    changed.iloc[0, changed.columns.get_loc(field)] += .5
    invalid = tmp_path / 'invalid'
    invalid.mkdir()
    snapshot, files = final_downloads(invalid, frame=changed)
    with pytest.raises(AssertionError, match='geometry/properties'):
        verify_case(current, uploads, snapshot, files, invalid)


@pytest.mark.parametrize('defect', ['same_count_wrong_id', 'map_wrong_geometry', 'cas_hash', 'not_final', 'wrong_crs'])
def test_oracle_rejects_plausible_but_wrong_results(tmp_path, defect):
    from shapely.geometry import Point
    from tests.browser.runtime_chinese_fixtures import build_uploads, expected_vector, verify_case
    current = case('vector_reproject_filter')
    uploads = build_uploads(current, tmp_path / 'uploads')
    result = expected_vector(current, uploads).to_crs(4326)
    map_result = result.copy()
    if defect == 'same_count_wrong_id':
        result.iloc[0, result.columns.get_loc('编号')] += 1
        map_result = result
    elif defect == 'map_wrong_geometry':
        map_result.iloc[0, map_result.columns.get_loc('geometry')] = Point(0, 0)
    elif defect == 'wrong_crs':
        result = result.to_crs(3857)
        map_result = result
    snapshot, files = final_downloads(tmp_path, frame=result, map_frame=map_result)
    if defect == 'cas_hash':
        snapshot['output_artifacts'][0]['content_hash'] = '0' * 64
    elif defect == 'not_final':
        snapshot['final_artifact_ids'] = ['unrelated']
    with pytest.raises(AssertionError):
        verify_case(current, uploads, snapshot, files, tmp_path)


@pytest.mark.parametrize('family', ['buffer', 'rename_buffer', 'buffer_ambiguous', 'sequential_buffers'])
def test_buffer_oracle_checks_filter_rename_metric_geometry_and_sequence(tmp_path, family):
    from tests.browser.runtime_chinese_fixtures import build_uploads, expected_vector, verify_case
    current = case(family)
    uploads = build_uploads(current, tmp_path / 'uploads')
    expected = expected_vector(current, uploads)
    metric = expected.to_crs(expected.estimate_utm_crs())
    metric.geometry = metric.buffer(100)
    result = metric.to_crs(4326)
    snapshot, files = final_downloads(tmp_path, frame=result)
    snapshot['tasks'] = [{'task_id': 'filter', 'op': 'attribute.filter', 'details': {'dependencies': []}}]
    before_buffer = 'filter'
    if family == 'rename_buffer':
        snapshot['tasks'].append({'task_id': 'rename', 'op': 'attribute.rename_field', 'details': {
            'parameters': {'old_name': '名称', 'new_name': '新名'}, 'dependencies': [{'task_id': 'filter'}]}})
        before_buffer = 'rename'
    if family == 'sequential_buffers':
        snapshot['tasks'] += [
            {'task_id': 'one', 'op': 'vector.buffer', 'details': {'parameters': {'distance_m': 25}, 'dependencies': [{'task_id': 'filter'}]}},
            {'task_id': 'two', 'op': 'vector.buffer', 'details': {'parameters': {'distance_m': 75}, 'dependencies': [{'task_id': 'one'}]}},
        ]
    else:
        snapshot['tasks'].append({'task_id': 'buffer', 'op': 'vector.buffer', 'details': {
            'parameters': {'distance_m': 100}, 'dependencies': [{'task_id': before_buffer}]}})
    assert verify_case(current, uploads, snapshot, files, tmp_path)['feature_count'] == 3
    if family == 'sequential_buffers':
        snapshot['tasks'][2]['details']['dependencies'] = [{'task_id': 'filter'}]
        with pytest.raises(AssertionError, match='depend'):
            verify_case(current, uploads, snapshot, files, tmp_path)
    else:
        current.params['distance_m'] = 250
        with pytest.raises(AssertionError, match='metric'):
            verify_case(current, uploads, snapshot, files, tmp_path)


@pytest.mark.parametrize('defect', [None, 'wrong_operation_order', 'nodata_lost', 'nodata_definition', 'grid_shift'])
def test_raster_oracle_checks_arithmetic_nodata_and_grid(tmp_path, defect):
    import numpy as np
    import rasterio
    from affine import Affine
    from tests.browser.runtime_chinese_fixtures import build_uploads, verify_case
    current = case('raster_calculate', 'tif')
    uploads = build_uploads(current, tmp_path / 'uploads')
    source = uploads[0].semantic_source
    values = np.asarray(source['values'], dtype='float32')
    output = values * 2 + 1 if defect != 'wrong_operation_order' else (values + 1) * 2
    nodata = -8888 if defect == 'nodata_definition' else -9999
    output[values == -9999] = 0 if defect == 'nodata_lost' else nodata
    transform = Affine(*source['transform'])
    if defect == 'grid_shift':
        transform *= Affine.translation(1, 0)
    path = tmp_path / 'final.tif'
    with rasterio.open(path, 'w', driver='GTiff', width=values.shape[1], height=values.shape[0],
                       count=1, dtype='float32', crs='EPSG:3857', transform=transform, nodata=nodata) as dataset:
        dataset.write(output, 1)
    snapshot, files = final_downloads(tmp_path, raster=path)
    if defect:
        with pytest.raises(AssertionError):
            verify_case(current, uploads, snapshot, files, tmp_path)
    else:
        assert verify_case(current, uploads, snapshot, files, tmp_path)['valid_pixels'] == values.size - 2


def test_api_rejects_gcj02_raster_without_publishing(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore, RuntimeSupervisor
    from tests.browser.runtime_chinese_fixtures import build_uploads
    runtime = RuntimeStore(RuntimeDB(tmp_path / 'runtime.db'))
    runtime.initialize()
    artifacts = ArtifactStore(tmp_path / 'artifacts', runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, isolate=False, autostart=False)
    upload = build_uploads(case('raster_calculate', 'tif'), tmp_path / 'uploads')[0]
    with TestClient(create_app(runtime_store=runtime, artifact_store=artifacts, runtime_supervisor=supervisor)) as client:
        response = client.post('/api/artifacts/uploads', files={'file': ('栅格.tif', upload.content_bytes, 'image/tiff')},
            data={'declared_crs': json.dumps({'kind': 'GCJ02'})})
    assert response.status_code == 422
    assert '栅格不支持 GCJ02' in response.json()['error']['message']
    assert runtime._read(lambda connection: connection.execute('SELECT count(*) FROM artifacts').fetchone()[0]) == 0
    assert not list(artifacts.staging_dir.iterdir())


def test_scheduled_profile_extremes_use_real_readable_formats(tmp_path):
    from tests.browser.runtime_chinese_corpus import load_cases
    from tests.browser.runtime_chinese_fixtures import build_uploads
    from app.runtime.upload_inspection import inspect_file
    # Every family, every point writer and the largest thresholds are exercised;
    # the other paraphrases intentionally share these physical profiles.
    selected = [value for value in load_cases() if value.paraphrase == 0 and value.profile in {0, 1, 2, 3, 4, 5, 19}]
    for current in selected:
        directory = tmp_path / current.case_id
        uploads = build_uploads(current, directory)
        for index, upload in enumerate(uploads):
            path = directory / f'content-{index}'
            path.write_bytes(upload.content_bytes)
            facts = inspect_file(path, upload.filename)
            assert facts['format'] == upload.physical_format
            if upload.physical_format not in {'csv', 'tsv'}:
                assert facts['crs'] == {'kind': 'EPSG', 'code': upload.semantic_source['epsg']}


@pytest.mark.parametrize('family', ['buffer', 'rename_buffer', 'vector_reproject_filter'])
def test_step_oracle_rejects_operations_in_wrong_order(family):
    from tests.browser.runtime_chinese_fixtures import verify_step_requirements
    current = case(family)
    task = {'task_id': 'result', 'op': 'vector.reproject' if family == 'vector_reproject_filter' else 'vector.buffer',
            'details': {'dependencies': [], 'parameters': {'target_crs': {'kind': 'EPSG', 'code': 4326}}}}
    snapshot = {'tasks': [task, {'task_id': 'filter', 'op': 'attribute.filter', 'details': {'dependencies': [{'task_id': 'result'}]}}]}
    with pytest.raises(AssertionError, match='filter must precede'):
        verify_step_requirements(current, snapshot)
    if family == 'rename_buffer':
        snapshot['tasks'][1]['details']['dependencies'] = []
        task['details']['dependencies'] = [{'task_id': 'filter'}]
        snapshot['tasks'].append({'task_id': 'rename', 'op': 'attribute.rename_field', 'details': {
            'parameters': {'old_name': '名称', 'new_name': '新名'}, 'dependencies': [{'task_id': 'result'}]}})
        with pytest.raises(AssertionError, match='rename must precede'):
            verify_step_requirements(current, snapshot)


@pytest.mark.parametrize('defect', ['extra_downloaded_vector', 'extra_omitted_vector', 'extra_coordinate', 'omitted_map'])
def test_oracle_rejects_unrequested_or_undownloaded_final_artifacts(tmp_path, defect):
    from tests.browser.runtime_chinese_fixtures import build_uploads, expected_vector, verify_case
    current = case('housing_multi')
    uploads = build_uploads(current, tmp_path / 'uploads')
    expected = expected_vector(current, uploads)
    snapshot, downloads = final_downloads(tmp_path, frame=expected)
    if defect == 'omitted_map':
        downloads.pop()
    else:
        extra = {'artifact_id': 'art_extra', 'type': 'Coordinate' if defect == 'extra_coordinate' else 'Vector',
                 'content_hash': snapshot['output_artifacts'][0]['content_hash']}
        snapshot['output_artifacts'].append(extra)
        snapshot['final_artifact_ids'].append(extra['artifact_id'])
        if defect != 'extra_omitted_vector':
            downloads.append({'artifact': extra, 'path': downloads[0]['path']})
    with pytest.raises(AssertionError):
        verify_case(current, uploads, snapshot, downloads, tmp_path)
