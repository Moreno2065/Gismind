import importlib
import json
import re

import pytest


@pytest.fixture
def harness():
    spec = importlib.util.find_spec('tests.live.prompt_e2e_2000')
    assert spec is not None, 'separate live API runner must exist'
    return importlib.import_module('tests.live.prompt_e2e_2000')


def test_exact_chinese_corpus(harness):
    cases = harness.load_cases()
    assert len(cases) == len({c.case_id for c in cases}) == len({c.prompt for c in cases}) == 2000
    assert all(re.search(r'[\u4e00-\u9fff]', c.prompt) and not re.search(r'[A-Za-z]', c.prompt) for c in cases)
    assert len({c.family for c in cases}) == 20
    assert {c.kind for c in cases} == {'success', 'negative', 'ambiguity'}
    assert harness.corpus_digest(cases) == harness.CORPUS_SHA256


def test_no_oracle_leak(harness):
    case = harness.load_cases()[0]
    payload = harness.request_payload(case, 'session-a', ['artifact-a'])
    assert payload['message'] == case.prompt
    assert set(payload) == {'session_id', 'message', 'input_artifact_ids', 'client_request_id'}


def test_checkpoint_is_pinned(harness, tmp_path):
    journal = harness.Journal(tmp_path / 'results.jsonl', 'abc')
    journal.append({'type': 'result', 'case_id': 'done', 'ok': True})
    assert 'done' in harness.Journal(journal.path, 'abc').completed
    with pytest.raises(ValueError, match='corpus'):
        harness.Journal(journal.path, 'different')


def test_torn_final_record_recovery(harness, tmp_path):
    journal = harness.Journal(tmp_path / 'results.jsonl', 'abc')
    journal.append({'type': 'result', 'case_id': 'done', 'ok': True})
    with journal.path.open('ab') as stream:
        stream.write(b'{')
    recovered = harness.Journal(journal.path, 'abc')
    assert 'done' in recovered.completed
    recovered.append({'type': 'result', 'case_id': 'next', 'ok': False})
    assert len(harness.Journal(journal.path, 'abc').completed) == 2


def test_missing_key_fails_closed(harness):
    with pytest.raises(ValueError, match='LLM_API_KEY'):
        harness.require_provider({'LLM_API_KEY': '', 'LLM_BASE_URL': 'https://example.invalid', 'LLM_MODEL': 'model'})


def test_wrong_geometry_cannot_pass(harness):
    case = next(c for c in harness.load_cases() if c.family == 'centroid')
    payload = {'type': 'FeatureCollection', 'features': harness.source_features(case)}
    with pytest.raises(AssertionError):
        harness.assert_semantics(case, [{'type': 'Vector', 'crs': dict(kind='EPSG', code=3857), 'payload': json.dumps(payload).encode()}])


def test_correct_centroid_geometry(harness):
    from shapely.geometry import mapping, shape
    case = next(c for c in harness.load_cases() if c.family == 'centroid')
    payload = {'type': 'FeatureCollection', 'features': [dict(f, geometry=mapping(shape(f['geometry']).centroid)) for f in harness.source_features(case)]}
    harness.assert_semantics(case, [{'type': 'Vector', 'crs': dict(kind='EPSG', code=3857), 'payload': json.dumps(payload).encode()}])


def test_compile_only_is_not_e2e(harness):
    with pytest.raises(AssertionError):
        harness.assert_live_provenance({'status': 'completed', 'tasks': []}, [], [], 'success')


def test_budget_survives_restart(harness, tmp_path):
    journal = harness.Journal(tmp_path / 'results.jsonl', 'abc')
    kwargs = dict(max_calls=2, max_tokens=1000, max_cost=1, reserve_tokens=400, usd_per_million=10, min_interval=0)
    budget = harness.CallBudget(journal, **kwargs)
    budget.reserve('a')
    budget.reserve('b')
    with pytest.raises(harness.BudgetExhausted):
        budget.reserve('c')
    with pytest.raises(harness.BudgetExhausted):
        harness.CallBudget(harness.Journal(journal.path, 'abc'), **kwargs).reserve('d')


def test_family_smoke_is_balanced_and_deterministic(harness):
    first = harness.select_cases(harness.load_cases(), 20, 20261002, True)
    assert len(first) == 20
    assert len({c.family for c in first}) == 20
    assert len({c.file_format for c in first}) == 5
    assert first == harness.select_cases(harness.load_cases(), 20, 20261002, True)


def test_single_family_selection_covers_every_frozen_rename_case(harness):
    cases = harness.load_cases()
    selected = harness.select_cases(cases, 2000, 20261002, family='rename')
    assert len(selected) == 100
    assert {c.case_id for c in selected} == {c.case_id for c in cases if c.family == 'rename'}
    assert {(c.profile, c.paraphrase) for c in selected} == {
        (profile, paraphrase) for profile in range(10) for paraphrase in range(10)
    }
    assert selected == harness.select_cases(cases, 2000, 20261002, family='rename')


def test_optional_family_filter_does_not_change_default_selection(harness):
    cases = harness.load_cases()
    assert harness.select_cases(cases, 2000, 20261002) == harness.select_cases(
        cases, 2000, 20261002, family=None
    )


def test_unknown_family_filter_fails_closed(harness):
    with pytest.raises(ValueError, match='unknown family'):
        harness.select_cases(harness.load_cases(), 2000, 20261002, family='renmae')


def test_unknown_family_cli_fails_before_provider_or_output(harness, tmp_path, capsys):
    output = tmp_path / 'must-not-exist'
    with pytest.raises(SystemExit) as captured:
        harness.main(['--live', '--family', 'renmae', '--max-cost-usd', '1', '--output', str(output)])
    assert captured.value.code == 2
    assert 'unknown family' in capsys.readouterr().err
    assert not output.exists()


def test_family_cli_rejects_combining_with_balanced_smoke(harness, tmp_path, capsys):
    output = tmp_path / 'must-not-exist'
    with pytest.raises(SystemExit) as captured:
        harness.main(['--live', '--family', 'rename', '--smoke-families', '--output', str(output)])
    assert captured.value.code == 2
    assert 'not allowed with argument' in capsys.readouterr().err
    assert not output.exists()


def test_compile_metrics_do_not_claim_unexecuted_stages(harness, tmp_path):
    journal = harness.Journal(tmp_path / 'results.jsonl', 'abc')
    case = harness.load_cases()[0]
    journal.append(dict(type='result', case_id=case.case_id, kind=case.kind, ok=False,
        stages=dict(api=True, planner=True, compiled=True, worker=False, artifact=False, semantic=False)))
    report = harness.summary(journal, [case])
    assert report['positive_e2e_passed'] == 0
    assert report['stages']['compiled'] == 1
    assert report['stages']['worker'] == report['stages']['semantic'] == 0


@pytest.mark.parametrize('family,profile', [('centroid', 0), ('buffer', 1), ('filter', 2), ('csvpoints', 0), ('rastercalc', 0)])
def test_real_upload_bytes_cross_public_api_offline(harness, tmp_path, family, profile):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore, RuntimeSupervisor
    case = next(c for c in harness.load_cases() if c.family == family and c.profile == profile)
    runtime = RuntimeStore(RuntimeDB(tmp_path / 'runtime.db'))
    runtime.initialize()
    artifacts = ArtifactStore(tmp_path / 'cas', runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, autostart=False)
    client = TestClient(create_app(runtime_store=runtime, artifact_store=artifacts, runtime_supervisor=supervisor))
    uploads = harness.build_uploads(case, tmp_path / 'fixtures')
    for name, data, crs in uploads:
        response = client.post('/api/artifacts/uploads', files={'file': (name, data)}, data={'declared_crs': json.dumps(crs)})
        assert response.status_code == 201, response.text
        output = client.get(response.json()['content_url'])
        assert output.content == data
    assert not runtime._read(lambda c: c.execute('SELECT * FROM planner_attempts').fetchall())
    client.close()


def test_provider_outage_cannot_pass_negative_case(harness):
    with pytest.raises(AssertionError):
        harness.assert_live_provenance({'status': 'failed'}, [{'provider': 'real', 'error_code': 'LLM_UNAVAILABLE'}], [], 'negative', [{'response_sha256': 'a'}])


def test_atomic_checkpoint_rejects_middle_corruption(harness, tmp_path):
    path = tmp_path / 'results.jsonl'
    journal = harness.Journal(path, 'abc')
    with path.open('ab') as stream:
        stream.write(b'garbage\n')
    with pytest.raises(ValueError, match='corrupt'):
        harness.Journal(path, 'abc')


@pytest.mark.parametrize('family', ['buffer', 'centroid', 'hull', 'bbox', 'explode', 'dedup', 'filter', 'keep', 'rename', 'calculate', 'reproject', 'dissolve', 'csvpoints', 'rastercalc', 'clip', 'merge'])
def test_each_positive_oracle_rejects_unmodified_input(harness, tmp_path, family):
    case = next(c for c in harness.load_cases() if c.family == family)
    if family == 'rastercalc':
        payload = harness.build_uploads(case, tmp_path)[0][1]
        artifact = dict(type='Raster', crs=dict(kind='EPSG', code=3857), payload=payload)
    else:
        payload = json.dumps(dict(type='FeatureCollection', features=harness.source_features(case))).encode()
        artifact = dict(type='Vector', crs=dict(kind='EPSG', code=3857), payload=payload)
    with pytest.raises((AssertionError, KeyError)):
        harness.assert_semantics(case, [artifact])


def test_preserved_properties_are_not_allowed_to_change(harness):
    from shapely.geometry import mapping, shape
    case = next(c for c in harness.load_cases() if c.family == 'centroid')
    features = [dict(f, geometry=mapping(shape(f['geometry']).centroid), properties=dict(f['properties'], 名称='错误名称')) for f in harness.source_features(case)]
    with pytest.raises(AssertionError):
        harness.assert_semantics(case, [dict(type='Vector', crs=dict(kind='EPSG', code=3857), payload=json.dumps(dict(type='FeatureCollection', features=features)).encode())])


def test_restart_with_different_selection_cannot_hide_manifest_drift(harness, tmp_path):
    path = tmp_path / 'results.jsonl'
    journal = harness.Journal(path, 'original')
    original = path.read_bytes()
    with pytest.raises(ValueError):
        harness.Journal(path, 'different')
    assert path.read_bytes() == original


def test_negative_missing_artifact_is_never_accepted(harness):
    with pytest.raises(AssertionError):
        harness.assert_live_provenance({'status': 'failed'},
            [{'provider': 'real', 'error_code': 'ARTIFACT_NOT_FOUND'}], [],
            'negative', [{'response_sha256': 'synthetic-probe'}], family='bad_csv')


def test_negative_requires_scenario_specific_input_diagnostic(harness):
    with pytest.raises(AssertionError):
        harness.assert_live_provenance({'status': 'failed'}, [{'provider': 'real'}],
            [{'error_code': 'PLAN_COMPILE_FAILED'}], 'negative',
            [{'response_sha256': 'a' * 64}], family='bad_csv')


def test_centroid_rejects_incorrect_crs_label(harness):
    from shapely.geometry import mapping, shape
    case = next(c for c in harness.load_cases() if c.family == 'centroid')
    features = [dict(f, geometry=mapping(shape(f['geometry']).centroid)) for f in harness.source_features(case)]
    with pytest.raises(AssertionError, match='CRS'):
        harness.assert_semantics(case, [dict(type='Vector', crs=dict(kind='EPSG', code=4326),
            payload=json.dumps(dict(type='FeatureCollection', features=features)).encode())])


def test_broken_upload_rejection_checks_specific_json_diagnostic(harness):
    case = next(c for c in harness.load_cases() if c.family == 'broken_vector')
    harness.assert_upload_rejection(case, 422, {'error': {'code': 'ARTIFACT_INVALID', 'message': 'Expecting value: line 1 column 39 (char 38)'}})
    with pytest.raises(AssertionError):
        harness.assert_upload_rejection(case, 422, {'error': {'code': 'ARTIFACT_INVALID', 'message': 'CRS mismatch'}})
    with pytest.raises(AssertionError):
        harness.assert_upload_rejection(case, 404, {'error': {'code': 'ARTIFACT_NOT_FOUND'}})


def test_checkpoint_rejects_changed_runner(harness, tmp_path, monkeypatch):
    path = tmp_path / 'results.jsonl'
    harness.Journal(path, 'abc')
    monkeypatch.setattr(harness, 'RUNNER_VERSION', 'changed')
    with pytest.raises(ValueError, match='runner'):
        harness.Journal(path, 'abc')


@pytest.mark.parametrize('wrong_metadata,wrong_grid', [(True, False), (False, True)])
def test_raster_rejects_wrong_georeferencing_with_correct_pixels(harness, wrong_metadata, wrong_grid):
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin
    case = next(c for c in harness.load_cases() if c.family == 'rastercalc')
    values = harness.raster_values(case)
    valid = values != -9999
    values[valid] += case.factor
    transform = from_origin(999999, 999999, 3, 4) if wrong_grid else from_origin(13000000, 3700000, 100, 100)
    with MemoryFile() as memory:
        with memory.open(driver='GTiff', width=case.count, height=case.count, count=1, dtype='float32', crs='EPSG:3857', transform=transform, nodata=-9999) as dataset:
            dataset.write(values, 1)
        artifact = dict(type='Raster', payload=memory.read(), crs=dict(kind='EPSG', code=4326 if wrong_metadata else 3857))
    with pytest.raises(AssertionError):
        harness.assert_semantics(case, [artifact])


def test_upload_rejection_is_separate_and_never_spends_provider_budget(harness, tmp_path):
    case = next(c for c in harness.load_cases() if c.family == 'broken_vector')
    journal = harness.Journal(tmp_path / 'results.jsonl', harness.CORPUS_SHA256)
    budget = harness.CallBudget(journal, max_calls=0, max_tokens=0, max_cost=0, reserve_tokens=0, usd_per_million=10, min_interval=0)
    result = harness.run_case(case, tmp_path / 'cases', journal, budget)
    assert result['ok'], result
    assert result['status'] == 'upload_rejected'
    assert result['stages']['planner'] is False
    assert result['stages']['worker'] is False
    assert not any(r['type'] == 'call_reserved' for r in journal.records)
    report = harness.summary(journal, [case])
    assert report['safe_upload_rejections'] == 1
    assert report['positive_e2e_passed'] == report['provider_response_count'] == 0


def test_usage_metadata_alone_is_not_actual_token_usage(harness, tmp_path):
    journal = harness.Journal(tmp_path / 'results.jsonl', 'abc')
    journal.append(dict(type='provider_response', case_id='case', usage=dict(planner_http_calls=1)))
    report = harness.summary(journal, [])
    assert report['actual_usage_complete'] is False
    assert report['actual_prompt_tokens'] is None


def test_missing_key_cli_fails_before_creating_run_or_calling_provider(harness, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app import config
    monkeypatch.setattr(config, 'get_settings', lambda: SimpleNamespace(LLM_API_KEY='', LLM_BASE_URL='https://example.invalid', LLM_MODEL='test'))
    output = tmp_path / 'must-not-exist'
    with pytest.raises(SystemExit) as captured:
        harness.main(['--live', '--limit', '1', '--max-cost-usd', '1', '--output', str(output)])
    assert captured.value.code == 2
    assert not output.exists()


def test_durable_worker_rejection_is_available_for_negative_oracle(harness):
    from types import SimpleNamespace
    def fail(*args, **kwargs):
        raise RuntimeError('COORDINATE_OUT_OF_RANGE')
    supervisor = SimpleNamespace(run_once=fail)
    client = SimpleNamespace(get=lambda path: SimpleNamespace(json=lambda: {'status': 'failed'}))
    assert harness.advance_run(supervisor, client, 'run', 'worker') is None
    client = SimpleNamespace(get=lambda path: SimpleNamespace(json=lambda: {'status': 'running'}))
    with pytest.raises(RuntimeError, match='COORDINATE_OUT_OF_RANGE'):
        harness.advance_run(supervisor, client, 'run', 'worker')


def test_runtime_fingerprint_tracks_sources_and_public_config_not_secrets(harness, tmp_path):
    from types import SimpleNamespace
    source = tmp_path / 'app'
    source.mkdir()
    entry = source / 'main.py'
    entry.write_text('VALUE = 1\n', encoding='utf-8')
    first_config = SimpleNamespace(LLM_MODEL='model-a', APP_PLANNER_MAX_TOKENS=2048, LLM_API_KEY='secret-a')
    first = harness.runtime_fingerprint(source, first_config, prompt_version='v1', case_timeout=180)
    assert first == harness.runtime_fingerprint(source, first_config, prompt_version='v1', case_timeout=180)
    first_config.LLM_API_KEY = 'secret-b'
    assert first == harness.runtime_fingerprint(source, first_config, prompt_version='v1', case_timeout=180)
    assert 'secret-' not in json.dumps(first)
    first_config.APP_PLANNER_MAX_TOKENS = 4096
    assert first['fingerprint'] != harness.runtime_fingerprint(source, first_config, prompt_version='v1', case_timeout=180)['fingerprint']
    first_config.APP_PLANNER_MAX_TOKENS = 2048
    entry.write_text('VALUE = 2\n', encoding='utf-8')
    assert first['fingerprint'] != harness.runtime_fingerprint(source, first_config, prompt_version='v1', case_timeout=180)['fingerprint']


def test_runtime_manifest_rejects_changed_production_and_marks_drift_invalid(harness, tmp_path):
    journal = harness.Journal(tmp_path / 'results.jsonl', 'abc')
    journal.append(dict(type='provider_manifest', runtime=dict(fingerprint='first')))
    harness.validate_runtime_manifest(journal, dict(fingerprint='first'))
    with pytest.raises(ValueError, match='runtime'):
        harness.validate_runtime_manifest(journal, dict(fingerprint='changed'))
    journal.append(dict(type='runtime_drift', expected='first', observed='changed'))
    assert harness.summary(journal, [])['evidence_valid'] is False
    with pytest.raises(ValueError, match='drift'):
        harness.validate_runtime_manifest(journal, dict(fingerprint='first'))


def test_centroid_geometry_and_properties_must_remain_bound(harness):
    from shapely.geometry import mapping, shape
    case = next(c for c in harness.load_cases() if c.family == 'centroid')
    original = harness.source_features(case)
    features = [dict(f, geometry=mapping(shape(f['geometry']).centroid), properties=original[(i + 1) % len(original)]['properties']) for i, f in enumerate(original)]
    with pytest.raises(AssertionError):
        harness.assert_semantics(case, [dict(type='Vector', crs=dict(kind='EPSG', code=3857),
            payload=json.dumps(dict(type='FeatureCollection', features=features)).encode())])


def test_buffer_diamond_is_not_a_circular_buffer(harness):
    from pyproj import Geod, Transformer
    from shapely.geometry import Polygon, mapping
    case = next(c for c in harness.load_cases() if c.family == 'buffer')
    to_wgs = Transformer.from_crs(3857, 4326, always_xy=True)
    from_wgs = Transformer.from_crs(4326, 3857, always_xy=True)
    geod = Geod(ellps='WGS84')
    features = []
    for feature in harness.source_features(case):
        lng, lat = to_wgs.transform(*feature['geometry']['coordinates'])
        vertices = [from_wgs.transform(*geod.fwd(lng, lat, bearing, case.distance)[:2]) for bearing in (0, 90, 180, 270)]
        features.append(dict(feature, geometry=mapping(Polygon(vertices))))
    with pytest.raises(AssertionError):
        harness.assert_semantics(case, [dict(type='Vector', crs=dict(kind='EPSG', code=3857),
            payload=json.dumps(dict(type='FeatureCollection', features=features)).encode())])


def test_batch_runtime_change_stops_admission_and_invalidates_evidence(harness, tmp_path):
    journal = harness.Journal(tmp_path / 'results.jsonl', 'abc')
    baseline = dict(fingerprint='stable')
    assert harness.check_runtime_batch(journal, baseline, lambda: dict(fingerprint='stable')) is True
    assert harness.check_runtime_batch(journal, baseline, lambda: dict(fingerprint='modified')) is False
    assert journal.records[-1]['type'] == 'runtime_drift'
    assert harness.summary(journal, [])['evidence_valid'] is False


def _delivery_fixture(harness, delivery='Vector', family='centroid'):
    from shapely.geometry import mapping, shape
    case = next(c for c in harness.load_cases() if c.family == family)
    features = harness.source_features(case)
    if family == 'centroid':
        features = [dict(f, geometry=mapping(shape(f['geometry']).centroid)) for f in features]
    payload = dict(type='FeatureCollection', features=features,
        crs=dict(type='name', properties=dict(name='EPSG:3857')))
    crs = dict(kind='EPSG', code=3857)
    if delivery == 'MapLayer':
        payload = dict(crs=crs, layers=[payload])
    return case, dict(type=delivery, crs=crs, payload=json.dumps(payload).encode())


@pytest.mark.parametrize('delivery', ['File', 'MapLayer'])
def test_equivalent_terminal_spatial_delivery_is_checked_numerically(harness, delivery):
    case, artifact = _delivery_fixture(harness, delivery)
    harness.assert_semantics(case, [artifact])


@pytest.mark.parametrize('delivery', ['File', 'MapLayer'])
@pytest.mark.parametrize('mutation', ['source_geometry', 'wrong_coordinate', 'swapped_properties', 'wrong_crs', 'empty'])
def test_equivalent_delivery_does_not_relax_spatial_or_record_assertions(harness, delivery, mutation):
    case, artifact = _delivery_fixture(harness, delivery)
    payload = json.loads(artifact['payload'])
    fc = payload['layers'][0] if delivery == 'MapLayer' else payload
    if mutation == 'source_geometry':
        fc['features'] = harness.source_features(case)
    elif mutation == 'wrong_coordinate':
        fc['features'][0]['geometry']['coordinates'][0] += 50
    elif mutation == 'swapped_properties':
        fc['features'][0]['properties'], fc['features'][1]['properties'] = fc['features'][1]['properties'], fc['features'][0]['properties']
    elif mutation == 'wrong_crs':
        artifact['crs'] = dict(kind='EPSG', code=4326)
    else:
        fc['features'] = []
    artifact['payload'] = json.dumps(payload).encode()
    with pytest.raises(AssertionError):
        harness.assert_semantics(case, [artifact])


@pytest.mark.parametrize('mutation', ['wrong_map_crs', 'extra_layer', 'empty_layers', 'reference_only', 'wrong_bbox'])
def test_map_delivery_requires_one_actual_consistent_spatial_layer(harness, mutation):
    case, artifact = _delivery_fixture(harness, 'MapLayer')
    payload = json.loads(artifact['payload'])
    if mutation == 'wrong_map_crs':
        payload['crs'] = dict(kind='EPSG', code=4326)
    elif mutation == 'extra_layer':
        payload['layers'].append(payload['layers'][0])
    elif mutation == 'empty_layers':
        payload['layers'] = []
    elif mutation == 'reference_only':
        payload['layers'] = [dict(artifact_id='unrelated-correct-intermediate')]
    else:
        payload['bbox'] = [0, 0, 1, 1]
    artifact['payload'] = json.dumps(payload).encode()
    with pytest.raises(AssertionError):
        harness.assert_semantics(case, [artifact])


def test_correct_terminal_does_not_hide_another_wrong_terminal(harness):
    case, correct = _delivery_fixture(harness)
    _, wrong = _delivery_fixture(harness, 'File', family='keep')
    with pytest.raises(AssertionError):
        harness.assert_semantics(case, [wrong, correct])


def _overlay_fixture(harness):
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union
    case = next(c for c in harness.load_cases() if c.family == 'clip')
    mask = unary_union([shape(f['geometry']) for f in harness.source_features(case, second=True)])
    features = [dict(f, geometry=mapping(shape(f['geometry']).intersection(mask)),
        properties={k + '_1': v for k, v in f['properties'].items()}) for f in harness.source_features(case)]
    return case, dict(type='Vector', crs=dict(kind='EPSG', code=3857),
        payload=json.dumps(dict(type='FeatureCollection', features=features)).encode())


def test_clip_overlay_suffix_preserves_source_record_binding(harness):
    case, artifact = _overlay_fixture(harness)
    harness.assert_semantics(case, [artifact])


@pytest.mark.parametrize('mutation', ['swapped', 'missing', 'wrong_value', 'wrong_geometry', 'conflicting_plain'])
def test_clip_overlay_suffix_does_not_hide_source_data_loss(harness, mutation):
    case, artifact = _overlay_fixture(harness)
    payload = json.loads(artifact['payload'])
    first, second = payload['features'][:2]
    if mutation == 'swapped':
        first['properties'], second['properties'] = second['properties'], first['properties']
    elif mutation == 'missing':
        del first['properties']['名称_1']
    elif mutation == 'wrong_value':
        first['properties']['数值_1'] = -999
    elif mutation == 'wrong_geometry':
        first['geometry'] = harness.source_features(case)[0]['geometry']
    else:
        first['properties'].update(harness.source_features(case)[0]['properties'])
        first['properties']['数值_1'] = -999
    artifact['payload'] = json.dumps(payload).encode()
    with pytest.raises(AssertionError):
        harness.assert_semantics(case, [artifact])


def test_overlay_suffix_is_not_a_general_rename_alias(harness):
    case, artifact = _delivery_fixture(harness)
    payload = json.loads(artifact['payload'])
    for feature in payload['features']:
        feature['properties'] = {k + '_1': v for k, v in feature['properties'].items()}
    artifact['payload'] = json.dumps(payload).encode()
    with pytest.raises(AssertionError):
        harness.assert_semantics(case, [artifact])


def _adjudicator():
    spec = importlib.util.find_spec('tests.live.adjudicate_prompt_e2e_2000')
    assert spec is not None, 'offline immutable-evidence adjudicator must exist'
    return importlib.import_module('tests.live.adjudicate_prompt_e2e_2000')


def _retained_case(tmp_path, artifact, *, correct_intermediate=None):
    import hashlib
    import sqlite3
    base = tmp_path / 'attempt-01'
    data = base / 'data'
    data.mkdir(parents=True)
    connection = sqlite3.connect(base / 'runtime.db')
    connection.executescript('''
        CREATE TABLE runs(run_id TEXT,status TEXT);
        CREATE TABLE plans(plan_id TEXT,run_id TEXT);
        CREATE TABLE tasks(task_id TEXT,plan_id TEXT,status TEXT,tool_name TEXT);
        CREATE TABLE task_outputs(task_id TEXT,artifact_id TEXT);
        CREATE TABLE task_inputs(task_id TEXT,artifact_id TEXT);
        CREATE TABLE artifacts(artifact_id TEXT,producer_task_id TEXT,type TEXT,crs TEXT,content_hash TEXT,storage_uri TEXT,validation_status TEXT);
        CREATE TABLE planner_attempts(run_id TEXT,status TEXT,provider TEXT,response_hash TEXT);
        CREATE TABLE task_attempts(task_id TEXT,status TEXT,executor_identity TEXT);
        INSERT INTO runs VALUES ('run','completed');
        INSERT INTO plans VALUES ('plan','run');
        INSERT INTO planner_attempts VALUES ('run','succeeded','unit-fixture','response');
    ''')
    for index, value in enumerate([correct_intermediate, artifact] if correct_intermediate else [artifact]):
        aid, tid = f'art-{index}', f'task-{index}'
        digest = hashlib.sha256(value['payload']).hexdigest()
        (data / digest).write_bytes(value['payload'])
        connection.execute('INSERT INTO artifacts VALUES (?,?,?,?,?,?,?)',
            (aid, tid, value['type'], json.dumps(value['crs']), digest, digest, 'valid'))
        connection.execute('INSERT INTO tasks VALUES (?,?,?,?)', (tid, 'plan', 'succeeded', 'fixture.operation'))
        connection.execute('INSERT INTO task_outputs VALUES (?,?)', (tid, aid))
        connection.execute('INSERT INTO task_attempts VALUES (?,?,?)', (tid, 'succeeded', json.dumps(dict(pid=987654321, create_time=1790928828.0))))
        if index:
            connection.execute('INSERT INTO task_inputs VALUES (?,?)', (tid, f'art-{index-1}'))
    connection.commit()
    connection.close()
    return base


def test_adjudicator_reads_only_terminal_outputs_and_keeps_source_bytes(harness, tmp_path):
    adjudicator = _adjudicator()
    import hashlib
    case, correct = _delivery_fixture(harness)
    _, wrong = _delivery_fixture(harness, 'File', family='keep')
    base = _retained_case(tmp_path, wrong, correct_intermediate=correct)
    before = hashlib.sha256((base / 'runtime.db').read_bytes()).hexdigest()
    artifacts = adjudicator.read_terminal_artifacts(base, 'run')
    assert len(artifacts) == 1 and artifacts[0]['artifact_id'] == 'art-1'
    with pytest.raises(AssertionError):
        harness.assert_semantics(case, artifacts)
    assert hashlib.sha256((base / 'runtime.db').read_bytes()).hexdigest() == before


@pytest.mark.parametrize('mutation', ['content_hash', 'invalid_status', 'upload_only', 'other_run', 'escape_path'])
def test_adjudicator_rejects_untrusted_or_unrelated_retained_output(harness, tmp_path, mutation):
    adjudicator = _adjudicator()
    import sqlite3
    _, artifact = _delivery_fixture(harness, 'File')
    base = _retained_case(tmp_path, artifact)
    connection = sqlite3.connect(base / 'runtime.db')
    if mutation == 'content_hash':
        connection.execute("UPDATE artifacts SET content_hash='incorrect'")
    elif mutation == 'invalid_status':
        connection.execute("UPDATE artifacts SET validation_status='invalid'")
    elif mutation == 'upload_only':
        connection.execute('DELETE FROM task_outputs')
    elif mutation == 'other_run':
        connection.execute("UPDATE plans SET run_id='other'")
    else:
        connection.execute("UPDATE artifacts SET storage_uri='../outside'")
    connection.commit()
    connection.close()
    with pytest.raises(AssertionError):
        adjudicator.read_terminal_artifacts(base, 'run')


def test_adjudicator_keeps_original_verdict_and_emits_separate_summary(harness, tmp_path):
    adjudicator = _adjudicator()
    import hashlib
    case, artifact = _delivery_fixture(harness, 'File')
    source = tmp_path / 'frozen'
    base = _retained_case(source / 'cases' / case.case_id, artifact)
    records = [dict(type='manifest', corpus_sha256=harness.CORPUS_SHA256, runner_version='old'),
        dict(type='provider_response', case_id=case.case_id, response_sha256='response'),
        dict(type='result', case_id=case.case_id, kind='success', family=case.family, run_id='run',
            ok=False, status='completed', failure_stage='semantic', failure_detail='missing final Vector artifact')]
    journal = source / 'results.jsonl'
    journal.write_text('\n'.join(json.dumps(r) for r in records)+'\n', encoding='utf-8')
    (source / 'summary.json').write_text(json.dumps(dict(completed=1, failed=1, passed=0, evidence_valid=True)), encoding='utf-8')
    before = hashlib.sha256(journal.read_bytes()).hexdigest()
    output = tmp_path / 'adjudicated'
    report = adjudicator.adjudicate(source, output)
    assert report['original_failed'] == report['corrected_false_negatives'] == 1
    assert report['adjudicated_failed'] == report['provider_calls_made'] == 0
    row = json.loads((output / 'adjudication.jsonl').read_text(encoding='utf-8'))
    assert row['original_ok'] is False and row['adjudicated_ok'] is True
    assert hashlib.sha256(journal.read_bytes()).hexdigest() == before
    with pytest.raises((ValueError, FileExistsError)):
        adjudicator.adjudicate(source, output)
    with pytest.raises(ValueError):
        adjudicator.adjudicate(source, source / 'overwrite')


def test_historical_worker_pid_reuse_does_not_change_retained_verdict(harness, tmp_path, monkeypatch):
    adjudicator = _adjudicator()
    case, artifact = _delivery_fixture(harness, 'File')
    base = _retained_case(tmp_path, artifact)
    monkeypatch.setattr(harness.os, 'getpid', lambda: 987654321)
    outputs = adjudicator._recheck(case, dict(run_id='run'), base, [dict(response_sha256='response')])
    assert len(outputs) == 1
    with pytest.raises(AssertionError, match='child process'):
        harness.assert_isolated_workers([dict(executor_identity=json.dumps(dict(pid=987654321, create_time=1790928828.0)))])


def test_live_budget_reserves_actual_provider_specific_response_format(harness):
    import ast
    import hashlib
    import inspect
    from types import SimpleNamespace
    tree = ast.parse(inspect.getsource(harness.run_case))
    planner_class = next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef))
    response_format = dict(type='json_schema', schema=dict(candidate_specific='x' * 100000))
    class OfflineTransport:
        max_tokens = 1024
        model = 'offline-test'
        provider = 'offline-test'
        def _response_format(self, messages):
            return response_format
        def _call(self, messages):
            return dict(usage={}), 0
    reservations = []
    namespace = dict(OpenAICompatiblePlanner=OfflineTransport, canonical=harness.canonical,
        hashlib=hashlib, case=SimpleNamespace(case_id='offline'),
        budget=SimpleNamespace(reserve=lambda *args, **kwargs: reservations.append(kwargs)),
        journal=SimpleNamespace(append=lambda record: None))
    exec(compile(ast.Module(body=[planner_class], type_ignores=[]), '<offline-metered-planner>', 'exec'), namespace)
    messages = [dict(role='user', content='离线预算测试')]
    namespace[planner_class.name]()._call(messages)
    expected = len(harness.canonical(messages).encode('utf-8')) + len(harness.canonical(response_format).encode('utf-8')) + 8192 + 1024
    assert reservations == [dict(calls=2, tokens=expected)]
