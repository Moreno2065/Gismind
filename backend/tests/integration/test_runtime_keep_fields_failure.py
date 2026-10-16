"""Real isolated GIS failures remain durable and publish no lossy artifacts."""
import json

from app.runtime import (ArtifactStore, ArtifactRef, Compiler, PlannerDraft,
                         PlannerStepDraft, RuntimeDB, RuntimeStore,
                         RuntimeSupervisor, ToolContractRegistry)


def test_missing_keep_field_fails_in_isolated_worker_before_publication(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / 'runtime.sqlite'))
    runtime.initialize()
    runtime.create_session('session')
    runtime.create_run('run', 'session')
    artifacts = ArtifactStore(tmp_path / 'cas', runtime)
    crs = {'kind': 'EPSG', 'code': 4326}
    payload = {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': {'名称': '站点', '数值': 12},
         'geometry': {'type': 'Point', 'coordinates': [118.7, 32.0]}},
    ]}
    source = artifacts.publish_staged(artifacts.stage_bytes(
        json.dumps(payload, ensure_ascii=False).encode('utf-8')),
        origin='upload', artifact_type='Vector', crs=crs,
        schema={'fields': {'名称': 'string', '数值': 'number'}})
    registry = ToolContractRegistry.with_builtins()
    plan = Compiler(registry, artifact_lookup=runtime.get_artifact).compile(PlannerDraft(
        run_id='run', input_revision=1, tasks=[PlannerStepDraft(
            task_key='projection', op='attribute.keep_fields',
            inputs={'input': ArtifactRef(artifact_id=source['artifact_id'], type='Vector', crs=crs)},
            params={'fields': ['数值', '新名']})]))
    runtime.create_plan('plan', 'run', plan=plan)
    runtime.create_task('projection', 'plan', 'projection', tool_name='attribute.keep_fields')
    supervisor = RuntimeSupervisor(runtime, artifacts, registry=registry, isolate=True, autostart=False)
    assert supervisor.worker_loop('worker', run_id='run', max_tasks=1, poll_interval=.001) == 1
    assert runtime._read(lambda db: db.execute(
        "SELECT status FROM tasks WHERE task_id='projection'").fetchone()[0]) == 'failed'
    assert runtime._read(lambda db: db.execute(
        "SELECT COUNT(*) FROM task_outputs WHERE task_id='projection'").fetchone()[0]) == 0
    assert runtime._read(lambda db: db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0]) == 1
    failures = [event for event in runtime.list_events('run') if event['event_type'] == 'task.failed']
    assert failures and '新名' in json.dumps(failures, ensure_ascii=False)
