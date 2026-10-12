import json

import pytest

from app.runtime import ArtifactRef, ArtifactStore, PlannerDraft, PlannerStepDraft, RuntimeDB, RuntimeStore, RuntimeSupervisor
from app.runtime.compiler import AwaitingInputDiagnostic, CompileError
from app.runtime.llm_planner import PlannerCall, PlannerError


def test_missing_business_fact_never_invokes_model_repair(tmp_path, monkeypatch):
    class Planner:
        repairs = 0

        def create_draft(self, request):
            return PlannerDraft(run_id=request.run_id, input_revision=1, user_goal=request.user_goal,
                                tasks=[PlannerStepDraft(task_key="locate", op="geo.geocode", params={"address": "南京"})])

        def repair_draft(self, request, **kwargs):
            self.repairs += 1
            return self.create_draft(request)

    def missing_fact(self, draft):
        raise AwaitingInputDiagnostic("CRS_REQUIRED", "inputs.file", "必须声明坐标系", {"patch_schema": {"type": "object"}})

    monkeypatch.setattr("app.runtime.queue.Compiler.compile", missing_fact)
    store = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    store.initialize()
    store.create_session("session")
    store.create_run("run", "session")
    store.add_input_revision("run", 1, {"prompt": "查询南京", "input_artifact_ids": []})
    planner = Planner()
    supervisor = RuntimeSupervisor(store, ArtifactStore(tmp_path / "data", store), planner=planner, autostart=False)
    assert supervisor.run_once("worker", run_id="run")["status"] == "awaiting_input"
    assert planner.repairs == 0
    assert store._read(lambda c: c.execute("SELECT COUNT(*) FROM planner_attempts WHERE run_id='run'").fetchone()[0]) == 1


def _audited_call(request, *, ordinal=1, clarification=False):
    return PlannerCall(
        draft=PlannerDraft(
            run_id=request.run_id, input_revision=request.input_revision,
            user_goal=request.user_goal, origin='needs_clarification' if clarification else 'llm',
            tasks=[] if clarification else [PlannerStepDraft(
                task_key='locate', op='geo.geocode', params={'address': '南京' if ordinal == 1 else '上海'})],
        ),
        provider='audit-provider', model='audit-model', prompt_version='audit-prompt',
        catalog_hash='c' * 64, request_hash=str(ordinal) * 64,
        response_hash=('a' if ordinal == 1 else 'b') * 64,
        usage={'prompt_tokens': 100 * ordinal, 'completion_tokens': 10 * ordinal},
        latency_ms=25 * ordinal,
    )


def _audit_runtime(tmp_path, planner):
    store = RuntimeStore(RuntimeDB(tmp_path / 'runtime.db'))
    store.initialize()
    store.create_session('session')
    store.create_run('run', 'session')
    store.add_input_revision('run', 1, {'prompt': '查询南京', 'input_artifact_ids': []})
    supervisor = RuntimeSupervisor(store, ArtifactStore(tmp_path / 'data', store),
        planner=planner, autostart=False)
    return store, supervisor


def _audit_attempts(store):
    return store._read(lambda c: [dict(row) for row in c.execute(
        "SELECT * FROM planner_attempts WHERE run_id='run' ORDER BY attempt_no")])


def _assert_call_retained(attempt, ordinal, *, clarification=False):
    assert attempt['response_hash'] == ('a' if ordinal == 1 else 'b') * 64
    assert json.loads(attempt['usage_json']) == {'prompt_tokens': 100 * ordinal, 'completion_tokens': 10 * ordinal}
    assert attempt['latency_ms'] == 25 * ordinal
    saved = json.loads(attempt['draft_json'])
    assert saved['run_id'] == 'run'
    assert saved['user_goal'] == '查询南京'
    assert saved['origin'] == ('needs_clarification' if clarification else 'llm')
    if clarification:
        assert saved['tasks'] == []
    else:
        assert saved['tasks'][0]['params']['address'] == ('南京' if ordinal == 1 else '上海')


@pytest.mark.parametrize('rejection', ['model_clarification', 'compiler_clarification', 'compiler_rejection'])
def test_rejected_valid_planner_call_keeps_auditable_response(tmp_path, monkeypatch, rejection):
    class Planner:
        def create_draft(self, request):
            return _audited_call(request, clarification=rejection == 'model_clarification')

    def reject(self, draft):
        if rejection == 'compiler_clarification':
            raise AwaitingInputDiagnostic('CRS_REQUIRED', 'inputs.file', '需要坐标系')
        raise CompileError('INVALID_PARAMETER', 'params', '参数非法')

    monkeypatch.setattr('app.runtime.queue.Compiler.compile', reject)
    store, supervisor = _audit_runtime(tmp_path, Planner())
    result = supervisor.run_once('worker', run_id='run')
    assert result['status'] == ('planning_failed' if rejection == 'compiler_rejection' else 'awaiting_input')
    attempts = _audit_attempts(store)
    assert len(attempts) == 1 and attempts[0]['status'] == 'failed'
    _assert_call_retained(attempts[0], 1, clarification=rejection == 'model_clarification')


@pytest.mark.parametrize('repair_result', ['provider_failure', 'clarification', 'compile_failure'])
def test_repair_attempt_records_only_its_own_response(tmp_path, monkeypatch, repair_result):
    class Planner:
        repairs = 0

        def create_draft(self, request):
            return _audited_call(request)

        def repair_draft(self, request, **kwargs):
            self.repairs += 1
            assert kwargs['rejected_draft'].tasks[0].params['address'] == '南京'
            if repair_result == 'provider_failure':
                raise PlannerError('LLM_UNAVAILABLE', 'provider unavailable')
            return _audited_call(request, ordinal=2, clarification=repair_result == 'clarification')

    def reject(self, draft):
        raise CompileError('INVALID_PARAMETER', 'params', '参数非法')

    monkeypatch.setattr('app.runtime.queue.Compiler.compile', reject)
    planner = Planner()
    store, supervisor = _audit_runtime(tmp_path, planner)
    result = supervisor.run_once('worker', run_id='run')
    assert result['status'] == ('awaiting_input' if repair_result == 'clarification' else 'planning_failed')
    attempts = _audit_attempts(store)
    assert planner.repairs == 1 and len(attempts) == 2
    assert [a['phase'] for a in attempts] == ['initial', 'repair']
    _assert_call_retained(attempts[0], 1)
    if repair_result == 'provider_failure':
        assert attempts[1]['response_hash'] is None
        assert attempts[1]['draft_json'] is None
        assert json.loads(attempts[1]['usage_json']) == {}
        assert attempts[1]['latency_ms'] is None
    else:
        _assert_call_retained(attempts[1], 2, clarification=repair_result == 'clarification')


def test_initial_provider_failure_does_not_fabricate_response_evidence(tmp_path):
    class Planner:
        def create_draft(self, request):
            raise PlannerError('LLM_UNAVAILABLE', 'provider unavailable')

    store, supervisor = _audit_runtime(tmp_path, Planner())
    assert supervisor.run_once('worker', run_id='run')['status'] == 'planning_failed'
    attempts = _audit_attempts(store)
    assert len(attempts) == 1
    assert attempts[0]['response_hash'] is None
    assert attempts[0]['draft_json'] is None
    assert json.loads(attempts[0]['usage_json']) == {}
    assert attempts[0]['latency_ms'] is None


def test_schema_repair_clarification_retains_only_valid_repair_call(tmp_path):
    class Planner:
        repairs = 0

        def create_draft(self, request):
            raise PlannerError('LLM_DRAFT_INVALID', 'invalid draft shape')

        def repair_draft(self, request, **kwargs):
            self.repairs += 1
            assert kwargs['rejected_draft'] is None
            return _audited_call(request, ordinal=2, clarification=True)

    planner = Planner()
    store, supervisor = _audit_runtime(tmp_path, planner)
    assert supervisor.run_once('worker', run_id='run')['status'] == 'awaiting_input'
    attempts = _audit_attempts(store)
    assert planner.repairs == 1 and len(attempts) == 2
    assert attempts[0]['draft_json'] is None and attempts[0]['response_hash'] is None
    _assert_call_retained(attempts[1], 2, clarification=True)


@pytest.mark.parametrize('question', ['', '   ', '\t\n', '问' * 1001, 123])
def test_clarification_question_rejects_blank_oversized_or_nonstring(question):
    with pytest.raises(ValueError):
        PlannerDraft(origin='needs_clarification', tasks=[], clarification=question)


def test_clarification_question_is_trimmed_and_legacy_omission_stays_valid():
    question = '请提供目标坐标系。'
    draft = PlannerDraft(origin='needs_clarification', tasks=[], clarification='  ' + question + '  ')
    assert draft.clarification == question
    assert PlannerDraft(origin='needs_clarification', tasks=[]).clarification is None
    assert PlannerDraft(origin='needs_clarification', tasks=[], clarification='问' * 1000).clarification == '问' * 1000


@pytest.mark.parametrize('origin,with_tasks', [('llm', False), ('planner', False), ('llm', True), ('needs_clarification', True)])
def test_clarification_question_cannot_be_mixed_with_executable_plan(origin, with_tasks):
    tasks = [PlannerStepDraft(task_key='locate', op='geo.geocode', params={'address': '南京'})] if with_tasks else []
    with pytest.raises(ValueError):
        PlannerDraft(origin=origin, tasks=tasks, clarification='需要哪个城市？')


def _question_runtime(tmp_path):
    class Planner:
        repairs = 0

        def create_draft(self, request):
            return PlannerDraft(run_id=request.run_id, user_goal=request.user_goal,
                origin='needs_clarification', tasks=[], clarification='请提供缓冲距离，单位为米。')

        def repair_draft(self, request, **kwargs):
            self.repairs += 1
            raise AssertionError('a concrete question must await user input, not model repair')

    planner = Planner()
    store, supervisor = _audit_runtime(tmp_path, planner)
    assert supervisor.run_once('worker', run_id='run')['status'] == 'awaiting_input'
    assert planner.repairs == 0
    return store, supervisor


def test_concrete_clarification_question_reaches_durable_snapshot_and_schema(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app

    store, supervisor = _question_runtime(tmp_path)
    with TestClient(create_app(runtime_store=store, artifact_store=supervisor.artifacts,
                              runtime_supervisor=supervisor)) as client:
        snapshot = client.get('/api/runs/run').json()
    question = '请提供缓冲距离，单位为米。'
    assert snapshot['status'] == 'awaiting_input'
    assert snapshot['diagnostic']['message'] == question
    assert snapshot['awaiting_input']['message'] == question
    assert snapshot['patch_schema'] == {
        'type': 'object',
        'properties': {'clarification': {'type': 'string', 'title': '补充信息',
            'description': question, 'minLength': 1, 'maxLength': 2000}},
        'required': ['clarification'], 'additionalProperties': False,
    }
    attempts = _audit_attempts(store)
    assert len(attempts) == 1
    assert json.loads(attempts[0]['draft_json'])['clarification'] == question
    assert not store._read(lambda c: c.execute("SELECT * FROM tasks").fetchall())


@pytest.mark.parametrize('patch', [
    {'clarification': ''},
    {'clarification': '答' * 2001},
    {'clarification': '二百米', 'unexpected': 'extra'},
    {'clarification': 200},
    {},
])
def test_resume_rejects_invalid_clarification_patch(tmp_path, patch):
    from fastapi.testclient import TestClient
    from app.main import create_app

    store, supervisor = _question_runtime(tmp_path)
    with TestClient(create_app(runtime_store=store, artifact_store=supervisor.artifacts,
                              runtime_supervisor=supervisor)) as client:
        response = client.post('/api/runs/run/resume', json={'command_id': 'invalid-answer', 'patch': patch})
    assert response.status_code == 422, response.text
    assert response.json()['error']['code'] == 'PATCH_SCHEMA_INVALID'
    assert store.get_run('run')['status'] == 'awaiting_input'


def test_resume_accepts_concrete_clarification_answer(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app

    store, supervisor = _question_runtime(tmp_path)
    with TestClient(create_app(runtime_store=store, artifact_store=supervisor.artifacts,
                              runtime_supervisor=supervisor)) as client:
        response = client.post('/api/runs/run/resume', json={
            'command_id': 'valid-answer', 'patch': {'clarification': '缓冲距离为二百米。'}})
    assert response.status_code == 202, response.text
    assert response.json()['status'] == 'queued'
    assert store.get_run('run')['status'] == 'queued'
    assert len(_audit_attempts(store)) == 1


def test_legacy_clarification_uses_existing_fallback_schema():
    draft = PlannerDraft(origin='needs_clarification', tasks=[])
    with pytest.raises(AwaitingInputDiagnostic) as captured:
        RuntimeSupervisor._require_executable_draft(draft)
    assert captured.value.message == 'planner requires additional user input before creating a plan'
    assert captured.value.details['patch_schema'] == {'type': 'object', 'minProperties': 1}


@pytest.mark.parametrize(('prompt', 'model_target', 'question_fragment'), [
    ('需要将属性表中的名称列换成新名列。', '新名列', '目标字段名'),
    ('字段名称不合适，改叫新名并输出图层。', '新名', '原字段名'),
    ('保留所有记录，将字段名称更名为新名。', '新名', '原字段名'),
])
def test_ambiguous_rename_cannot_execute_a_real_planner_draft_without_user_choice(
    tmp_path, prompt, model_target, question_fragment,
):
    class Planner:
        def create_draft(self, request):
            return PlannerCall(
                draft=PlannerDraft(
                    run_id=request.run_id, input_revision=request.input_revision,
                    user_goal=request.user_goal, origin='llm', tasks=[PlannerStepDraft(
                        task_key='rename', op='attribute.rename_field',
                        params={'old_name': '名称', 'new_name': model_target},
                    )],
                ),
                provider='deepseek', model='deepseek-flash', prompt_version='live-boundary-test',
                catalog_hash='c' * 64, request_hash='d' * 64, response_hash='e' * 64,
                usage={'prompt_tokens': 90, 'completion_tokens': 25}, latency_ms=37,
            )

    store = RuntimeStore(RuntimeDB(tmp_path / 'runtime.db'))
    store.initialize()
    store.create_session('session')
    artifacts = ArtifactStore(tmp_path / 'data', store)
    uploaded = artifacts.publish_staged(
        artifacts.stage_bytes(b'{"type":"FeatureCollection","features":[]}', filename='source.geojson'),
        origin='upload', artifact_type='File', media_type='application/geo+json',
        schema={'inspection': {'artifact_type': 'Vector', 'fields': {'名称': 'str', '数值': 'int'}}},
    )
    store.create_run('run', 'session')
    store.add_input_revision('run', 1, {
        'prompt': prompt,
        'input_artifact_ids': [uploaded['artifact_id']],
    })
    supervisor = RuntimeSupervisor(store, artifacts, planner=Planner(), autostart=False)

    assert supervisor.run_once('worker', run_id='run')['status'] == 'awaiting_input'
    snapshot = store.get_run('run')
    assert snapshot['status'] == 'awaiting_input'
    from app.main import create_app
    from fastapi.testclient import TestClient
    with TestClient(create_app(runtime_store=store, artifact_store=artifacts,
                               runtime_supervisor=supervisor)) as client:
        public_snapshot = client.get('/api/runs/run').json()
    assert public_snapshot['diagnostic']['code'] == 'CLARIFICATION_REQUIRED'
    assert question_fragment in public_snapshot['awaiting_input']['message']
    assert not store._read(lambda c: c.execute('SELECT * FROM plans WHERE run_id=?', ('run',)).fetchall())
    assert not store._read(lambda c: c.execute('SELECT * FROM tasks').fetchall())
    attempts = store._read(lambda c: [dict(row) for row in c.execute(
        'SELECT * FROM planner_attempts WHERE run_id=?', ('run',))])
    assert len(attempts) == 1
    assert attempts[0]['response_hash'] == 'e' * 64
    assert json.loads(attempts[0]['usage_json']) == {'prompt_tokens': 90, 'completion_tokens': 25}
    assert json.loads(attempts[0]['draft_json'])['tasks'][0]['params']['new_name'] == model_target


@pytest.mark.parametrize(('prompt', 'answer', 'expected_source', 'expected_target'), [
    ('需要将属性表中的名称列换成新名列。', '新名', '名称', '新名'),
    ('字段名称不合适，改叫新名并输出图层。', '数值', '数值', '新名'),
])
def test_rename_clarification_resume_binds_the_answer_before_compilation(
    tmp_path, prompt, answer, expected_source, expected_target,
):
    store = RuntimeStore(RuntimeDB(tmp_path / 'runtime.db'))
    store.initialize()
    store.create_session('session')
    artifacts = ArtifactStore(tmp_path / 'data', store)
    uploaded = artifacts.publish_staged(
        artifacts.stage_bytes(b'{"type":"FeatureCollection","features":[]}', filename='source.geojson'),
        origin='upload', artifact_type='Vector', media_type='application/geo+json',
        crs={'kind': 'EPSG', 'code': 4326}, schema={'fields': {'名称': 'str', '数值': 'int'}},
    )

    class Planner:
        def create_draft(self, request):
            choice = request.revision_patch.get('clarification')
            source = expected_source if choice else '名称'
            target = expected_target if choice else ('新名列' if prompt.endswith('新名列。') else '新名')
            return PlannerDraft(
                run_id=request.run_id, input_revision=request.input_revision,
                user_goal=request.user_goal, origin='llm', tasks=[PlannerStepDraft(
                    task_key='rename', op='attribute.rename_field',
                    inputs={'input': ArtifactRef(artifact_id=uploaded['artifact_id'], type='Vector',
                                                 crs={'kind': 'EPSG', 'code': 4326})},
                    params={'old_name': source, 'new_name': target},
                )],
            )

    store.create_run('run', 'session')
    store.add_input_revision('run', 1, {'prompt': prompt, 'input_artifact_ids': [uploaded['artifact_id']]})
    supervisor = RuntimeSupervisor(store, artifacts, planner=Planner(), autostart=False)
    assert supervisor.run_once('worker', run_id='run')['status'] == 'awaiting_input'
    from app.main import create_app
    from fastapi.testclient import TestClient
    with TestClient(create_app(runtime_store=store, artifact_store=artifacts,
                               runtime_supervisor=supervisor)) as client:
        response = client.post('/api/runs/run/resume', json={
            'command_id': 'answer-rename', 'patch': {'clarification': answer}})
    assert response.status_code == 202, response.text
    assert supervisor.run_once('worker', run_id='run')['status'] == 'planned'
    rows = store._read(lambda c: [dict(row) for row in c.execute(
        'SELECT * FROM tasks WHERE plan_id IN (SELECT plan_id FROM plans WHERE run_id=?)', ('run',))])
    assert len(rows) == 1
    params = json.loads(rows[0]['task_json'])['params']
    assert params == {'old_name': expected_source, 'new_name': expected_target}
