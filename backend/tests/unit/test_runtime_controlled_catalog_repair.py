"""Offline real-planner parser/queue tests; these are not live provider evidence."""
import json

import pytest

from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore, RuntimeSupervisor, ToolContractRegistry
import app.runtime.llm_planner as boundary


def catalog(registry, names):
    return boundary._make_catalog(names, [boundary._contract_summary(registry.get(name)) for name in names])


def request(goal='计算质心', kind='File'):
    return boundary.PlannerRequest(run_id='r', input_revision=1, user_goal=goal,
        input_artifacts=({'artifact_id': 'source', 'type': kind},), revision_patch={})


def expand(registry, req, base, operation, code='OPERATION_NOT_IN_CATALOG'):
    helper = getattr(boundary, 'controlled_repair_catalog', None)
    assert callable(helper), 'production repair catalog expansion seam is missing'
    details = {'operation': operation} if code == 'OPERATION_NOT_IN_CATALOG' else {'requirement': {'op': operation}}
    return helper(registry, req, {'code': code, 'details': details}, base=base)


def test_controlled_expansion_includes_required_loader_and_changes_hash():
    registry = ToolContractRegistry.with_builtins()
    base = catalog(registry, ['geo.geocode'])
    expanded = expand(registry, request(), base, 'vector.centroid')
    assert {'geo.geocode', 'io.load_vector', 'vector.centroid'} <= set(expanded.operation_ids)
    assert expanded.catalog_hash != base.catalog_hash and expanded.expansion_parent_hash == base.catalog_hash
    assert len(expanded) <= 18


@pytest.mark.parametrize('operation', ['unregistered.operation', 'code.execute', 'publish_file'])
def test_model_named_unknown_or_unrequested_sensitive_operation_cannot_widen_catalog(operation):
    registry = ToolContractRegistry.with_builtins()
    base = catalog(registry, ['geo.geocode'])
    assert expand(registry, request(), base, operation).catalog_hash == base.catalog_hash


def test_explicit_side_effect_request_can_expand_but_negation_cannot():
    registry = ToolContractRegistry.with_builtins()
    base = catalog(registry, ['geo.geocode'])
    assert 'publish_file' in expand(registry, request('发布文件到指定路径'), base, 'publish_file').operation_ids
    assert expand(registry, request('不要发布文件到指定路径'), base, 'publish_file').catalog_hash == base.catalog_hash


def test_expansion_never_evicts_initial_operations_to_fit_loader_closure():
    registry = ToolContractRegistry.with_builtins()
    names = [contract.name for contract in registry.values() if contract.name not in {'vector.centroid', 'io.load_vector', 'code.execute', 'publish_file'}][:17]
    base = catalog(registry, names)
    result = expand(registry, request(), base, 'vector.centroid')
    assert result.catalog_hash == base.catalog_hash


@pytest.mark.parametrize('initial_kind', ['missing_goal', 'outside_catalog', 'missing_goal_full'])
def test_real_planner_queue_repair_uses_expanded_prompt_hash_in_attempt_and_ir(tmp_path, monkeypatch, initial_kind):
    registry = ToolContractRegistry.with_builtins()
    base_names = ['vector.centroid']
    if initial_kind == 'missing_goal_full':
        base_names += [contract.name for contract in registry.values() if contract.name not in {'vector.centroid', 'vector.point_on_surface', 'code.execute', 'publish_file'}][:17]
    base = catalog(registry, base_names)
    monkeypatch.setattr(boundary, 'candidate_catalog', lambda *args, **kwargs: base)
    monkeypatch.setattr('app.runtime.queue.candidate_catalog', lambda *args, **kwargs: base)
    runtime = RuntimeStore(RuntimeDB(tmp_path / 'runtime.db'))
    runtime.initialize()
    runtime.create_session('s')
    runtime.create_run('r', 's')
    artifacts = ArtifactStore(tmp_path / 'data', runtime)
    data = {'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'properties': {'name': 'A'}, 'geometry': {'type': 'Polygon', 'coordinates': [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}}]}
    source = artifacts.publish_staged(artifacts.stage_bytes(json.dumps(data).encode()), origin='upload', artifact_type='Vector', crs={'kind': 'EPSG', 'code': 4326}, schema={'fields': {'name': 'string'}})
    runtime.add_input_revision('r', 1, {'prompt': '计算面内点', 'input_artifact_ids': [source['artifact_id']]})
    planner = boundary.OpenAICompatiblePlanner(api_key='offline-test', base_url='https://provider.invalid', model='offline-test', timeout_seconds=1, max_tokens=1000, max_retries=0, registry=registry)
    calls = []

    def offline_transport(messages):
        payload = json.loads(messages[-1]['content'])
        calls.append(payload)
        op = 'vector.centroid' if len(calls) == 1 and initial_kind.startswith('missing_goal') else 'vector.point_on_surface'
        draft = {'tasks': [{'task_key': 'result', 'op': op, 'inputs': {'input': {'kind': 'artifact', 'artifact_id': source['artifact_id']}}, 'params': {}}]}
        return {'choices': [{'message': {'content': json.dumps(draft)}}], 'usage': {}}, 1

    monkeypatch.setattr(planner, '_call', offline_transport)
    outcome = RuntimeSupervisor(runtime, artifacts, registry=registry, planner=planner, isolate=False).run_once('offline-worker', run_id='r')
    if initial_kind == 'missing_goal_full':
        assert outcome['status'] == 'planning_failed'
        assert len(calls) == 1, 'do not pay for an impossible repair with the same full catalog'
        attempts = runtime._read(lambda conn: [dict(row) for row in conn.execute('SELECT * FROM planner_attempts')])
        assert len(attempts) == 1 and attempts[0]['error_code'] == 'GOAL_REQUIREMENT_MISSING'
        return
    assert outcome['status'] == 'planned', 'known missing operation must be made available in the existing one repair'
    assert len(calls) == 2
    assert 'vector.point_on_surface' not in calls[0]['operation_catalog']['operation_ids']
    assert 'vector.point_on_surface' in calls[1]['operation_catalog']['operation_ids']
    expected = calls[1]['operation_catalog']['catalog_hash']
    assert expected != base.catalog_hash
    attempts = runtime._read(lambda conn: [dict(row) for row in conn.execute('SELECT * FROM planner_attempts ORDER BY attempt_no')])
    assert len(attempts) == 2 and attempts[0]['status'] == 'failed' and attempts[1]['status'] == 'succeeded'
    assert attempts[0]['catalog_hash'] == base.catalog_hash
    assert attempts[1]['catalog_hash'] == expected
    assert json.loads(attempts[1]['draft_json'])['catalog_hash'] == expected
    plan = runtime._read(lambda conn: json.loads(conn.execute('SELECT ir_json FROM plans').fetchone()[0]))
    assert plan['catalog_hash'] == expected
    assert plan['allowed_operation_ids'] == calls[1]['operation_catalog']['operation_ids']
