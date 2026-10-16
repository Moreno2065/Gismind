"""Offline harness safeguards; never counted as paid browser evidence."""
import importlib.util
import copy
import json
import os
import sys
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Point


@pytest.mark.parametrize('exit_code', [0, 7])
def test_owned_process_records_natural_exit_before_cleanup(tmp_path, exit_code):
    from tests.browser import runtime_live_browser as runner
    lifecycle = {}
    with runner.owned_process([sys.executable, '-c', f'raise SystemExit({exit_code})'],
                              tmp_path, os.environ.copy(), tmp_path / 'child.log',
                              lifecycle=lifecycle, name='frontend') as process:
        assert process.wait(timeout=10) == exit_code
        states = runner.process_states({'frontend': process}, phase='after_case', case_id='case1')
        assert states['processes']['frontend'] == {'pid': process.pid, 'alive': False, 'exit_code': exit_code}
        with pytest.raises(RuntimeError, match=f'frontend.*exit code {exit_code}'):
            runner.require_owned_servers(states)
    record = lifecycle['frontend']
    assert record['pid'] == process.pid
    assert record['before_cleanup']['exit_code'] == exit_code
    assert record['before_cleanup']['alive'] is False
    assert record['cleanup_requested'] is False
    assert record['final_exit_code'] == exit_code
    assert record['started_at'] <= record['before_cleanup']['observed_at'] <= record['finished_at']


def test_owned_process_distinguishes_harness_cleanup_from_early_exit(tmp_path):
    from tests.browser import runtime_live_browser as runner
    lifecycle = {}
    with runner.owned_process([sys.executable, '-c', 'import time; time.sleep(30)'],
                              tmp_path, os.environ.copy(), tmp_path / 'child.log',
                              lifecycle=lifecycle, name='backend') as process:
        states = runner.process_states({'backend': process}, phase='before_case', case_id='case1')
        assert states['phase'] == 'before_case' and states['case_id'] == 'case1'
        assert states['observed_at'].endswith('Z')
        runner.require_owned_servers(states)
    record = lifecycle['backend']
    assert record['before_cleanup']['alive'] is True
    assert record['before_cleanup']['exit_code'] is None
    assert record['cleanup_requested'] is True
    assert isinstance(record['final_exit_code'], int)
    assert process.poll() is not None


@pytest.mark.parametrize('defect', ['duplicate', 'property', 'missing_property', 'string_number'])
def test_map_download_oracle_rejects_same_count_wrong_multiset(tmp_path, defect):
    frame = gpd.GeoDataFrame({'id': ['A', 'B'], 'price': [280.0, 300.0], 'note': [None, 'ok']},
                             geometry=[Point(118.8, 32.05), Point(118.801, 32.051)], crs=4326)
    path = tmp_path / 'actual.gpkg'
    frame.to_file(path, driver='GPKG')
    features = json.loads(frame.to_json())['features']
    if defect == 'duplicate':
        features[1] = copy.deepcopy(features[0])
    elif defect == 'property':
        features[0]['properties']['price'] = 281
    elif defect == 'missing_property':
        del features[0]['properties']['note']
    else:
        features[0]['properties']['price'] = '280'
    with pytest.raises(AssertionError, match='map and (final download|download)'):
        support().verify_map_matches_download({'crs': {'kind': 'EPSG', 'code': 4326},
                                              'layers': [{'type': 'FeatureCollection', 'features': features}]}, path)


def test_map_download_oracle_allows_order_numeric_null_and_projected_roundtrip(tmp_path):
    source = gpd.GeoDataFrame({'id': ['A', 'B', 'C'], 'price': [280, 300, 300], 'note': [None, 'ok', None]},
                              geometry=[Point(118.8, 32.05), Point(118.801, 32.051), Point(118.801, 32.051)], crs=4326)
    path = tmp_path / 'actual.gpkg'
    source.iloc[::-1].to_crs(3857).to_file(path, driver='GPKG')
    features = json.loads(source.to_json())['features']
    for feature in features:
        feature['properties']['price'] = float(feature['properties']['price'])
    support().verify_map_matches_download({'crs': {'kind': 'WGS84'},
                                          'layers': [{'type': 'FeatureCollection', 'features': features}]}, path)


def test_seeded_housing_oracle_is_independent_and_boundary_sensitive(tmp_path):
    module = support()
    first = module.fixture_uploads('housing_seeded', seed=19)
    assert first == module.fixture_uploads('housing_seeded', seed=19)
    assert first != module.fixture_uploads('housing_seeded', seed=20)
    houses = gpd.GeoDataFrame.from_features(first[0][1]['features'], crs=4326)
    zone = gpd.GeoDataFrame.from_features(first[1][1]['features'], crs=4326).geometry.iloc[0]
    selected = houses[(houses['总价万元'] <= 300) & (houses['面积平方米'] >= 90) & houses.geometry.within(zone)]
    assert len(selected) == 3 and 'A' not in set(selected['房源ID'])
    assert ((selected['总价万元'] == 300) & (selected['面积平方米'] == 90)).any()
    path = tmp_path / 'selected.gpkg'
    selected.iloc[::-1].to_crs(3857).to_file(path, driver='GPKG')
    assert module.verify_download('housing_seeded', path, seed=19)['feature_count'] == 3
    wrong = tmp_path / 'omitted-boundary.gpkg'
    selected[selected['总价万元'] < 300].to_file(wrong, driver='GPKG')
    with pytest.raises(AssertionError, match='housing'):
        module.verify_download('housing_seeded', wrong, seed=19)


def test_projected_housing_fixture_declares_physical_coordinates():
    module = support()
    original = module.fixture_uploads('housing_seeded', seed=29)
    projected = module.fixture_uploads('housing_projected', seed=29)
    for (_, geographic), (_, physical) in zip(original, projected):
        assert physical['crs']['properties']['name'] == 'EPSG:3857'
        left = gpd.GeoDataFrame.from_features(geographic['features'], crs=4326)
        right = gpd.GeoDataFrame.from_features(physical['features'], crs=3857).to_crs(4326)
        assert all(a.equals_exact(b, tolerance=1e-7) for a, b in zip(left.geometry, right.geometry))


def test_live_schedule_keeps_baselines_and_bounds_opt_in_repeats():
    from tests.browser import runtime_live_browser as runner
    assert support().BASELINE_CASES == ('buffer', 'multi_step', 'housing', 'housing_clarify')
    schedule = getattr(runner, 'schedule_cases', None)
    assert callable(schedule), 'live repeat schedule must be explicit and bounded'
    records = schedule(['housing_seeded', 'housing_projected'], seed=17, repeat=2)
    assert len(records) == 4
    assert {row['seed'] for row in records} == {17, 18}
    assert len({row['evidence_id'] for row in records}) == 4
    with pytest.raises(ValueError):
        schedule(['housing_seeded'], seed=17, repeat=100)


@pytest.mark.parametrize('defect', ['completed', 'different_run', 'stale_version', 'hidden'])
def test_active_recovery_oracle_cannot_pass_from_finished_history(defect):
    from tests.browser import runtime_live_browser as runner
    check = getattr(runner, 'verify_recovery', None)
    assert callable(check), 'active recovery must be checked independently of final completion'
    before = {'run_id': 'run1', 'session_id': 'session1', 'status': 'planning', 'run_version': 2}
    after = {**before}
    observations = [{'run_id': 'run1', 'visible': True, 'stage': '正在规划', 'tasks': []}]
    if defect == 'completed':
        after['status'] = 'completed'
    elif defect == 'different_run':
        after['run_id'] = 'run2'
    elif defect == 'stale_version':
        after['run_version'] = 1
    else:
        observations[0]['visible'] = False
    with pytest.raises(AssertionError):
        check(before, after, kind='planning_refresh', observations=observations)


def test_awaiting_recovery_requires_same_patch_context_and_run_version():
    from tests.browser import runtime_live_browser as runner
    check = getattr(runner, 'verify_recovery', None)
    assert callable(check), 'awaiting recovery must preserve resumable context'
    before = {'run_id': 'run1', 'session_id': 'session1', 'status': 'awaiting_input', 'run_version': 3,
              'awaiting_input': {'question': '条件？', 'patch_schema': {'required': ['answer']}}}
    check(before, dict(before), kind='awaiting_refresh', observations=[])
    with pytest.raises(AssertionError, match='context'):
        check(before, {**before, 'awaiting_input': None}, kind='awaiting_refresh', observations=[])


def test_evidence_seal_detects_modified_and_added_files(tmp_path):
    from tests.browser import runtime_live_browser as runner
    seal = getattr(runner, 'seal_evidence', None)
    verify = getattr(runner, 'verify_evidence_seal', None)
    assert callable(seal) and callable(verify), 'finished live evidence requires a verifiable byte manifest'
    (tmp_path / 'result.json').write_text('{"passed":false}', encoding='utf-8')
    seal(tmp_path)
    verify(tmp_path)
    (tmp_path / 'result.json').write_text('{"passed":true}', encoding='utf-8')
    with pytest.raises(AssertionError, match='evidence'):
        verify(tmp_path)
    (tmp_path / 'result.json').write_text('{"passed":false}', encoding='utf-8')
    (tmp_path / 'later.txt').write_text('extra', encoding='utf-8')
    with pytest.raises(AssertionError, match='evidence'):
        verify(tmp_path)


def support():
    path = Path(__file__).parents[1] / 'browser' / 'runtime_live_browser_support.py'
    assert path.exists(), 'real browser harness support is missing'
    spec = importlib.util.spec_from_file_location('live_browser_support', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_housing_oracle_rejects_missing_attribute_and_spatial_constraints(tmp_path):
    check = support().verify_download
    source = gpd.GeoDataFrame({'房源ID': ['A', 'B'], '总价万元': [280, 350], '面积平方米': [100, 110]}, geometry=[Point(118.80, 32.05), Point(118.801, 32.051)], crs=4326)
    path = tmp_path / 'bad.gpkg'
    source.to_file(path, driver='GPKG')
    with pytest.raises(AssertionError, match='housing IDs'):
        check('housing', path)
    source.iloc[:1].to_file(tmp_path / 'good.gpkg', driver='GPKG')
    assert check('housing', tmp_path / 'good.gpkg')['feature_count'] == 1


def test_buffer_oracle_rejects_geographic_degree_buffer(tmp_path):
    check = support().verify_download
    wrong = gpd.GeoDataFrame({'site_id': ['S1']}, geometry=[Point(118.8, 32.05).buffer(.00001)], crs=4326)
    path = tmp_path / 'bad.gpkg'
    wrong.to_file(path, driver='GPKG')
    with pytest.raises(AssertionError, match='500 m'):
        check('buffer', path)


def test_housing_fixtures_exercise_each_constraint():
    fixtures = support().fixture_uploads('housing')
    houses = fixtures[0][1]['features']
    assert {item['properties']['房源ID'] for item in houses} == {'A', 'B', 'C', 'D'}
    assert fixtures[1][1]['features'][0]['geometry']['type'] == 'Polygon'


def test_live_provider_provenance_requires_observed_hash_and_isolated_worker():
    check = support().verify_provenance
    evidence = {'responses': [{'response_hash': 'real', 'http_calls': 1}]}
    attempts = [{'provider': 'openai-compatible', 'model': 'deepseek-flash', 'response_hash': 'unobserved', 'status': 'succeeded'}]
    workers = [{'executor_identity': '{"pid": 42}', 'attempt_status': 'succeeded'}]
    with pytest.raises(AssertionError, match='observed provider'):
        check(attempts, workers, evidence, server_pid=9)
    attempts[0]['response_hash'] = 'real'
    check(attempts, workers, evidence, server_pid=9)
    with pytest.raises(AssertionError, match='isolated'):
        check(attempts, workers, evidence, server_pid=42)


def test_source_manifest_covers_backend_frontend_config_and_harness():
    hashes = support().source_hashes()
    for name in ['backend/app/main.py', 'frontend/src/components/ChatPanel.tsx', 'frontend/package-lock.json', '.github/workflows/runtime-live-e2e.yml', 'backend/tests/browser/runtime_live_browser.py']:
        assert name in hashes


def test_finished_planning_history_does_not_prove_visible_live_progress():
    from tests.browser.runtime_live_browser import has_visible_active_progress
    assert not has_visible_active_progress([{'visible': True, 'stage': '已完成', 'text': '生成计划 完成', 'tasks': []}])
    assert not has_visible_active_progress([{'visible': False, 'stage': '正在规划', 'tasks': []}])
    assert has_visible_active_progress([{'visible': True, 'stage': '正在修复计划', 'tasks': []}])
    assert has_visible_active_progress([{'visible': True, 'stage': '运行中', 'tasks': [{'status': 'validating'}]}])


def test_live_progress_order_respects_dependencies_not_alphabetical_keys():
    from tests.browser.runtime_live_browser import verify_step_order
    edges = [{'task_id': 'buffer', 'depends_on_task_id': 'load'},
             {'task_id': 'map', 'depends_on_task_id': 'buffer'}]
    with pytest.raises(AssertionError, match='dependency order'):
        verify_step_order(['buffer', 'load', 'map'], edges)
    verify_step_order(['load', 'buffer', 'map'], edges)


@pytest.mark.parametrize('defect', [None, 'closed', 'parameter', 'input', 'output'])
def test_expanded_task_details_match_persisted_snapshot(defect):
    from tests.browser import runtime_live_browser as runner
    check = getattr(runner, 'verify_task_detail_observation', None)
    assert callable(check), 'browser gate must inspect expanded persisted task details'
    task = {'task_id': 'buffer', 'details': {
        'parameters': {'distance_m': 500, 'target_crs': {'kind': 'EPSG', 'code': 3857}},
        'inputs': [{'name': 'vector', 'source_task_key': 'load', 'source_port': 'vector', 'source_ordinal': 0}],
        'resolved_inputs': [], 'outputs': [{'name': 'vector', 'ordinal': 0, 'artifact_id': 'result/id'}],
    }}
    observed = {'open': True, 'text': 'vector → load.vector[0]',
                'parameters': {'distance_m': '500', 'target_crs': '{"kind":"EPSG","code":3857}'},
                'links': ['/api/artifacts/result%2Fid/content']}
    if defect == 'closed':
        observed['open'] = False
    elif defect == 'parameter':
        observed['parameters']['distance_m'] = '50'
    elif defect == 'input':
        observed['text'] = 'vector → another.vector[0]'
    elif defect == 'output':
        observed['links'] = []
    if defect:
        with pytest.raises(AssertionError):
            check(observed, task)
    else:
        check(observed, task)


@pytest.mark.parametrize('defect', [None, 'empty', 'source', 'bounds', 'type'])
def test_sdk_overlay_evidence_is_compared_with_physical_download(tmp_path, defect):
    from tests.browser import runtime_live_browser as runner
    from app.tools.geo_transform import wgs84_to_gcj02

    check = getattr(runner, 'verify_rendered_geometry', None)
    assert callable(check), 'browser gate must verify actual SDK overlay geometry'
    path = tmp_path / 'housing.gpkg'
    gpd.GeoDataFrame({'房源ID': ['A']}, geometry=[Point(118.8, 32.05)], crs=4326).to_file(path, driver='GPKG')
    x, y = wgs84_to_gcj02(118.8, 32.05)
    evidence = {'observation': 'sdk-overlays', 'displayCrs': 'GCJ02', 'sourceArtifactId': 'final-map',
                'overlayCount': 1, 'geometryTypes': {'Point': 1, 'LineString': 0, 'Polygon': 0},
                'bounds': [x, y, x, y]}
    if defect == 'empty':
        evidence['overlayCount'] = 0
    elif defect == 'source':
        evidence['sourceArtifactId'] = 'original-upload'
    elif defect == 'bounds':
        evidence['bounds'] = [118.8, 32.05, 118.8, 32.05]
    elif defect == 'type':
        evidence['geometryTypes'] = {'Point': 0, 'LineString': 0, 'Polygon': 1}
    if defect:
        with pytest.raises(AssertionError):
            check(evidence, path, artifact_id='final-map')
    else:
        check(evidence, path, artifact_id='final-map')
