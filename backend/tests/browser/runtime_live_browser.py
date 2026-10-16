"""Opt-in real Chrome -> HTTP -> real LLM -> normal Runtime -> UI/download gate.

Use --inventory without network calls. Live mode needs a fresh evidence path,
an explicit budget and token prices. No response stubs or manually driven worker.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.parse import quote, urlsplit
from urllib.request import urlopen

from tests.browser.runtime_live_browser_support import (
    BASELINE_CASES, CASES, expected_housing, fixture_uploads, source_hashes, verify_download, verify_map_matches_download, verify_provenance,
)

BACKEND = Path(__file__).resolve().parents[2]
FRONTEND = BACKEND.parent / 'frontend'


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def schedule_cases(case_ids, *, seed, repeat):
    if not 1 <= repeat <= 5 or len(case_ids) * repeat > 20:
        raise ValueError('repeat must be 1..5 and total browser executions must not exceed 20')
    if len(set(case_ids)) != len(case_ids):
        raise ValueError('duplicate case names are not allowed; use --repeat')
    return [{'case_id': case_id, 'seed': seed + iteration,
             'evidence_id': case_id if repeat == 1 else f'{case_id}-r{iteration + 1}-s{seed + iteration}'}
            for iteration in range(repeat) for case_id in case_ids]


def evidence_hashes(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob('*')) if path.is_file() and path != root / 'evidence-manifest.json'}


def seal_evidence(root):
    path = root / 'evidence-manifest.json'
    with path.open('x', encoding='utf-8') as output:
        output.write(json.dumps(evidence_hashes(root), ensure_ascii=False, indent=2) + '\n')


def verify_evidence_seal(root):
    expected = json.loads((root / 'evidence-manifest.json').read_text(encoding='utf-8'))
    assert expected == evidence_hashes(root), 'evidence files changed after the byte manifest was sealed'


def free_port():
    with socket.socket() as connection:
        connection.bind(('127.0.0.1', 0))
        return connection.getsockname()[1]


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _process_state(process):
    exit_code = process.poll()
    return {'pid': process.pid, 'alive': exit_code is None, 'exit_code': exit_code}


def process_states(processes, *, phase, case_id=None):
    return {'observed_at': _utc_now(), 'phase': phase, 'case_id': case_id,
            'processes': {name: _process_state(process) for name, process in processes.items()}}


def require_owned_servers(observation):
    for name, state in observation['processes'].items():
        if not state['alive']:
            raise RuntimeError(f"owned {name} server exited before harness cleanup with exit code {state['exit_code']} "
                               f"(PID {state['pid']}, {observation['phase']}, {observation['observed_at']})")


@contextmanager
def owned_process(command, cwd, env, log, *, lifecycle=None, name=None):
    options = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    with log.open('wb') as output:
        started_at = _utc_now()
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **options)
        record = {'pid': process.pid, 'started_at': started_at, 'executable': str(command[0]), 'log': log.name}
        if lifecycle is not None:
            lifecycle[name or log.stem] = record
        try:
            yield process
        finally:
            record['before_cleanup'] = {**_process_state(process), 'observed_at': _utc_now()}
            record['cleanup_requested'] = record['before_cleanup']['alive']
            # Only this invocation's process tree is targeted, never an existing dev server.
            try:
                if record['cleanup_requested']:
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
                    else:
                        os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        if os.name != 'nt':
                            os.killpg(process.pid, signal.SIGKILL)
                        process.kill()
                        process.wait(timeout=5)
            finally:
                record['finished_at'] = _utc_now()
                record['final_exit_code'] = process.poll()


def wait_server(url, process, *, worker=False):
    deadline = time.monotonic() + 60
    last_error = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f'owned server exited with {process.returncode}; inspect its log')
        try:
            with urlopen(url, timeout=2) as response:
                body = response.read()
                if worker:
                    health = json.loads(body)
                    assert health.get('checks', {}).get('worker', {}).get('running'), 'normal supervisor is not running'
                    return health
                return {'http_status': response.status}
        except Exception as error:
            last_error = error
            time.sleep(.2)
    raise RuntimeError(f'owned server readiness failed: {last_error}')


def sql_rows(database, query, parameters=()):
    with sqlite3.connect(f'{database.as_uri()}?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query, parameters)]


def snapshot(context, base_url, run_id, headers):
    response = context.request.get(f'{base_url}/api/runs/{run_id}', headers=headers)
    assert response.status == 200, f'snapshot HTTP {response.status}'
    return response.json()


def wait_outcome(page, context, base_url, run_id, *, timeout, progress=None):
    deadline = time.monotonic() + timeout
    snapshots = []
    headers = {'X-User-Id': page.evaluate("localStorage.getItem('gismind.user_id')")}
    while time.monotonic() < deadline:
        value = snapshot(context, base_url, run_id, headers)
        if progress is not None:
            # MutationObserver captures fast stages between HTTP polling ticks.
            observed = page.evaluate('window.__runtimeProgress.splice(0)')
            for item in observed:
                if item['run_id'] == run_id and (not progress or progress[-1] != item):
                    progress.append(item)
        if not snapshots or snapshots[-1] != value:
            snapshots.append(value)
        if value['status'] in {'completed', 'failed', 'cancelled', 'awaiting_input'}:
            return value, snapshots
        page.wait_for_timeout(500)
    raise AssertionError(f'Run {run_id} did not reach a terminal or input state in {timeout}s')


def verify_visible_progress(page, value):
    """DOM step states must agree with the authoritative durable snapshot."""
    from playwright.sync_api import expect

    panel = page.get_by_label('任务运行状态', exact=True)
    expect(panel).to_have_attribute('data-run-id', value['run_id'])
    expect(panel).to_be_visible()
    rows = panel.locator('[data-task-id]')
    expect(rows).to_have_count(len(value['tasks']))
    assert rows.evaluate_all('nodes => nodes.map(row => row.dataset.taskId)') == [task['task_id'] for task in value['tasks']], 'frontend step order differs from snapshot'
    for task in value['tasks']:
        row = panel.locator(f'[data-task-id="{task["task_id"]}"]')
        expect(row).to_have_attribute('data-task-status', task['status'])
        expect(row).to_contain_text(task['task_key'])
        expect(row).to_have_attribute('data-task-op', task['op'])
    return panel.inner_text()


def verify_step_order(task_ids, dependencies):
    positions = {task_id: index for index, task_id in enumerate(task_ids)}
    for edge in dependencies:
        assert positions[edge['depends_on_task_id']] < positions[edge['task_id']], 'visible steps violate dependency order'


def verify_task_detail_observation(observed, task):
    """Compare rendered detail facts with the persisted snapshot, never model text."""
    details = task.get('details')
    assert isinstance(details, dict), 'snapshot lacks persisted task details'
    assert observed.get('open'), 'task details were not expanded'
    for name, value in details.get('parameters', {}).items():
        displayed = observed.get('parameters', {}).get(name)
        assert displayed is not None, f'task parameter {name} is not rendered'
        if not isinstance(value, str):
            try:
                displayed = json.loads(displayed)
            except (ValueError, TypeError):
                raise AssertionError(f'task parameter {name} is not structured JSON') from None
        assert displayed == value, f'task parameter {name} differs from snapshot'
    links = set(observed.get('links', []))
    for item in details.get('inputs', []):
        assert item['name'] in observed['text'], 'named input port is missing'
        if item.get('source_task_id') or item.get('source_task_key'):
            reference = f"{item.get('source_task_key') or item['source_task_id']}.{item['source_port']}[{('*' if item.get('selection') == 'all' else item.get('source_ordinal', 0))}]"
            assert reference in observed['text'], 'task input reference differs from snapshot'
        if item.get('artifact_id'):
            assert f"/api/artifacts/{quote(item['artifact_id'], safe='')}/content" in links, 'input artifact link is missing'
    for item in details.get('outputs', []) + details.get('resolved_inputs', []):
        assert f"/api/artifacts/{quote(item['artifact_id'], safe='')}/content" in links, 'persisted artifact link is missing'


def exercise_task_details(page, value, case_root, viewport_name):
    from playwright.sync_api import expect

    assert value['tasks'], 'cannot verify task details without persisted tasks'
    selected = [value['tasks'][0]]
    parameter_task = next((task for task in value['tasks'] if task.get('details', {}).get('parameters')), None)
    if parameter_task and parameter_task['task_id'] != selected[0]['task_id']:
        selected.append(parameter_task)
    observations = []
    for index, task in enumerate(selected):
        row = page.get_by_test_id('runtime-progress').locator(f'[data-task-id="{task["task_id"]}"]')
        details = row.get_by_test_id('task-execution-details')
        summary = details.locator('summary')
        summary.scroll_into_view_if_needed()
        if details.get_attribute('open') is None:
            summary.click()
        expect(details).to_have_attribute('open', '')
        expect(details).to_be_visible()
        observed = details.evaluate("""node => ({
            open: node.open, text: node.innerText,
            parameters: Object.fromEntries(Array.from(node.querySelectorAll('dl > div')).map(item => [item.querySelector('dt').textContent, item.querySelector('dd').textContent])),
            links: Array.from(node.querySelectorAll('a[href]')).map(link => link.getAttribute('href')),
        })""")
        verify_task_detail_observation(observed, task)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), 'expanded task details overflow horizontally'
        # Keep the summary and start of the expanded detail in the bounded list.
        details.evaluate("node => node.scrollIntoView({block: 'start'})")
        page.screenshot(path=str(case_root / f'{viewport_name}-task-details-{index + 1}.png'), full_page=True)
        observations.append({'task_id': task['task_id'], **observed})
        summary.click()
    return observations


def has_visible_active_progress(observations):
    return any(item.get('visible') and (
        any(label in (item.get('stage') or '') for label in ('正在规划', '正在修复计划', '执行中', '正在校验'))
        or any(task.get('status') in {'running', 'validating', 'leased'} for task in item.get('tasks', []))
    ) for item in observations)


def observe_live_progress(page):
    script = """(() => {
      const install = () => {
        window.__runtimeProgress = [];
        let previous = '';
        const record = () => {
            const panel = document.querySelector('[data-testid="runtime-progress"]');
            if (!panel) return;
            const observation = {
                run_id: panel.dataset.runId, visible: panel.getBoundingClientRect().height > 0,
                stage: panel.querySelector('[data-testid="runtime-stage"]')?.textContent,
                text: panel.textContent,
                tasks: Array.from(panel.querySelectorAll('[data-task-id]')).map(row => ({
                    task_id: row.dataset.taskId, status: row.dataset.taskStatus, op: row.dataset.taskOp,
                })),
            };
            const serialized = JSON.stringify(observation);
            if (serialized !== previous) {
                window.__runtimeProgress.push(observation);
                previous = serialized;
            }
        };
        new MutationObserver(record).observe(document.body, {subtree: true, childList: true, attributes: true, characterData: true});
        record();
      };
      if (document.body) install();
      else document.addEventListener('DOMContentLoaded', install, {once: true});
    })()"""
    page.add_init_script(script)
    page.evaluate(script)


def verify_recovery(before, after, *, kind, observations):
    assert before['run_id'] == after['run_id'] and before['session_id'] == after['session_id'], 'recovery changed active Run identity'
    assert after['run_version'] >= before['run_version'], 'recovery regressed Run version'
    if kind == 'awaiting_refresh':
        assert before['status'] == after['status'] == 'awaiting_input', 'awaiting-input recovery changed paused state'
        assert before['run_version'] == after['run_version'], 'refresh mutated paused Run version'
        assert before.get('awaiting_input') and before['awaiting_input'] == after.get('awaiting_input'), 'recovery lost input patch context'
    else:
        assert before['status'] == 'planning', 'recovery did not start during actual planning'
        assert after['status'] in {'planning', 'running'}, 'active recovery window missed; completed history does not prove recovery'
        assert has_visible_active_progress([item for item in observations if item.get('run_id') == before['run_id']]), 'recovery did not display active Run progress'


def exercise_recovery(page, context, base_url, run_id, root, case_root, report, kind):
    from playwright.sync_api import expect

    panel = page.get_by_label('任务运行状态', exact=True)
    expect(panel).to_have_attribute('data-run-id', run_id)
    stage = panel.get_by_test_id('runtime-stage')
    if kind.startswith('planning_'):
        expect(stage).to_contain_text('规划', timeout=30000)
    else:
        expect(stage).to_contain_text('等待补充信息', timeout=15000)
    expect(panel).to_contain_text('实时同步', timeout=30000)
    headers = {'X-User-Id': page.evaluate("localStorage.getItem('gismind.user_id')")}
    before = snapshot(context, base_url, run_id, headers)
    events_before = sql_rows(root / 'runtime.db', 'SELECT * FROM events WHERE run_id=? ORDER BY seq', (run_id,))
    report['ui_progress'].extend(page.evaluate('window.__runtimeProgress.splice(0)'))
    recovery = {'kind': kind, 'before': before, 'events_before': events_before}
    report['recovery'] = recovery
    write_json(case_root / 'recovery-before.json', recovery)
    page.screenshot(path=str(case_root / 'recovery-before.png'), full_page=True)
    with page.expect_response(lambda response: urlsplit(response.url).path == f'/api/runs/{run_id}/events'
                              and response.status == 200, timeout=30000):
        if kind == 'planning_reconnect':
            context.set_offline(True)
            try:
                expect(panel).to_contain_text('正在恢复连接', timeout=10000)
                recovery['disconnected_text'] = panel.inner_text()
            finally:
                context.set_offline(False)
        else:
            page.reload(wait_until='domcontentloaded')
    expect(panel).to_have_attribute('data-run-id', run_id, timeout=30000)
    expect(panel).to_be_visible()
    if kind == 'awaiting_refresh':
        expect(stage).to_contain_text('等待补充信息', timeout=15000)
        structured = page.locator('.runtime-input-form')
        if structured.count():
            expect(structured.get_by_role('button', name='提交补充信息')).to_be_enabled()
        else:
            expect(page.get_by_role('textbox', name='分析需求')).to_be_enabled()
    else:
        page.wait_for_function("""runId => {
            const panel = document.querySelector('[data-testid="runtime-progress"]');
            return panel && panel.dataset.runId === runId && panel.getBoundingClientRect().height > 0 &&
                (/正在规划|正在修复计划|执行中|正在校验/.test(panel.querySelector('[data-testid="runtime-stage"]')?.textContent || '') ||
                 Array.from(panel.querySelectorAll('[data-task-status]')).some(row => ['running','validating','leased'].includes(row.dataset.taskStatus)));
        }""", arg=run_id, timeout=30000)
    after = snapshot(context, base_url, run_id, headers)
    observations = page.evaluate('window.__runtimeProgress.splice(0)')
    report['ui_progress'].extend(observations)
    recovery.update(after=after, observations=observations, recovered_text=panel.inner_text())
    events_after = sql_rows(root / 'runtime.db', 'SELECT * FROM events WHERE run_id=? ORDER BY seq', (run_id,))
    recovery['events_after'] = events_after
    write_json(case_root / 'recovery-after.json', recovery)
    verify_recovery(before, after, kind=kind, observations=observations)
    assert events_after[:len(events_before)] == events_before, 'browser recovery changed immutable durable events'
    if kind == 'awaiting_refresh':
        assert events_after == events_before, 'refresh created a new paused-Run event'
    page.screenshot(path=str(case_root / 'recovery-after.png'), full_page=True)


def verify_rendered_geometry(evidence, downloaded, *, artifact_id):
    """Compare attached SDK geometry with an independent physical-file read."""
    import geopandas as gpd
    from app.tools.geo_transform import wgs84_to_gcj02

    assert evidence.get('observation') == 'sdk-overlays', 'map evidence was not observed from SDK overlays'
    assert evidence.get('sourceArtifactId') == artifact_id, 'map rendered a different source Artifact'
    assert evidence.get('displayCrs') == 'GCJ02', 'map display datum is not explicit'
    expected_types = {'Point': 0, 'LineString': 0, 'Polygon': 0}
    coordinates = []

    def collect(value):
        if len(value) >= 2 and isinstance(value[0], (int, float)):
            coordinates.append(wgs84_to_gcj02(value[0], value[1]))
        else:
            for child in value:
                collect(child)

    for geometry in gpd.read_file(downloaded).to_crs(4326).geometry:
        parts = list(geometry.geoms) if geometry.geom_type.startswith('Multi') else [geometry]
        for part in parts:
            assert part.geom_type in expected_types, 'unsupported geometry in SDK oracle'
            expected_types[part.geom_type] += 1
            collect(part.__geo_interface__['coordinates'])
    assert coordinates, 'download has no geometry to display'
    assert evidence.get('overlayCount') == sum(expected_types.values()), 'actual SDK overlay count differs from final download'
    assert evidence.get('geometryTypes') == expected_types, 'actual SDK geometry types differ from final download'
    x, y = zip(*coordinates)
    expected_bounds = [min(x), min(y), max(x), max(y)]
    observed = evidence.get('bounds')
    assert isinstance(observed, list) and len(observed) == 4, 'actual SDK geometry bounds are missing'
    assert all(isinstance(actual, (int, float)) and math.isfinite(actual)
               and math.isclose(actual, expected, rel_tol=0, abs_tol=2e-6)
               for actual, expected in zip(observed, expected_bounds)), 'actual SDK geometry bounds differ from final download'


def run_case(browser, base_url, case_id, root, server_pid, timeout, *, seed=0, evidence_id=None):
    from playwright.sync_api import expect

    case_root = root / (evidence_id or case_id)
    case_root.mkdir()
    report = {'case_id': case_id, 'evidence_id': case_root.name, 'seed': seed, 'passed': False,
              'prompt': CASES[case_id]['prompt'], 'uploads': [], 'ui_progress': []}
    started = time.monotonic()
    context = browser.new_context(viewport={'width': 1600, 'height': 1040}, accept_downloads=True, reduced_motion='reduce')
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    page = context.new_page()
    requests, responses, page_errors = [], [], []
    page.on('request', lambda request: requests.append({'method': request.method, 'path': urlsplit(request.url).path,
        **({'body': request.post_data} if request.method == 'POST' and urlsplit(request.url).path.startswith('/api/') else {})}))
    page.on('response', lambda response: responses.append({'path': urlsplit(response.url).path, 'status': response.status,
        'content_type': response.headers.get('content-type', '')}))
    page.on('pageerror', lambda error: page_errors.append(str(error)))
    try:
        page.goto(base_url, wait_until='networkidle')
        composer = page.get_by_role('textbox', name='分析需求')
        expect(composer).to_be_enabled(timeout=30000)
        for name, fixture in fixture_uploads(case_id, seed=seed):
            data = json.dumps(fixture, ensure_ascii=False).encode('utf-8')
            (case_root / name).write_bytes(data)
            with page.expect_response(lambda response: response.request.method == 'POST' and urlsplit(response.url).path == '/api/artifacts/uploads', timeout=60000) as pending:
                page.locator('input[type=file]').set_input_files({'name': name, 'mimeType': 'application/geo+json', 'buffer': data})
            response = pending.value
            assert response.status == 201, response.text()
            uploaded = response.json()
            assert uploaded['content_hash'] == hashlib.sha256(data).hexdigest()
            if CASES[case_id].get('input_crs'):
                assert uploaded.get('crs') == {'kind': 'EPSG', 'code': CASES[case_id]['input_crs']}, 'physical projected upload CRS was not preserved'
            report['uploads'].append(uploaded)
            expect(page.get_by_text(name + ' · 就绪', exact=False)).to_be_visible()
        if case_id.startswith('housing'):
            expected = expected_housing(case_id, seed=seed)
            report['expected_housing_ids'] = sorted(expected['房源ID'])
            write_json(case_root / 'expected-housing.json', json.loads(expected.to_json()))
        page.screenshot(path=str(case_root / '01-uploaded.png'), full_page=True)
        observe_live_progress(page)
        composer.fill(CASES[case_id]['prompt'])
        with page.expect_response(lambda response: response.request.method == 'POST' and urlsplit(response.url).path == '/api/runs', timeout=30000) as created:
            page.get_by_role('button', name='发送', exact=True).click()
        assert created.value.status == 202, created.value.text()
        run_id = created.value.json()['run_id']
        report['run_id'] = run_id
        recovery_kind = CASES[case_id].get('recovery', '')
        if recovery_kind.startswith('planning_'):
            exercise_recovery(page, context, base_url, run_id, root, case_root, report, recovery_kind)
        value, snapshots = wait_outcome(page, context, base_url, run_id, timeout=timeout, progress=report['ui_progress'])
        report['snapshots'] = snapshots
        if 'reply' in CASES[case_id]:
            assert value['status'] == 'awaiting_input', 'ambiguous housing request was executed or failed instead of asking'
            assert not value['output_artifacts'], 'ambiguous housing request produced output before clarification'
            assert not sql_rows(root / 'runtime.db', 'SELECT a.* FROM task_attempts a JOIN tasks t ON t.task_id=a.task_id JOIN plans p ON p.plan_id=t.plan_id WHERE p.run_id=?', (run_id,)), 'worker executed an unresolved housing request'
            expect(page.get_by_label('任务运行状态')).to_contain_text('等待补充信息', timeout=15000)
            page.screenshot(path=str(case_root / '02-awaiting-input.png'), full_page=True)
            if recovery_kind == 'awaiting_refresh':
                exercise_recovery(page, context, base_url, run_id, root, case_root, report, recovery_kind)
            reply = CASES[case_id]['reply']
            structured = page.locator('.runtime-input-form')
            with page.expect_response(lambda response: response.request.method == 'POST' and urlsplit(response.url).path.endswith('/resume'), timeout=30000) as resumed:
                if structured.count():
                    # A concrete textual clarification uses the published patch-schema control.
                    controls = structured.get_by_role('textbox')
                    assert controls.count() == 1, 'unexpected clarification schema needs explicit scenario coverage'
                    controls.fill(reply)
                    structured.get_by_role('button', name='提交补充信息').click()
                else:
                    expect(composer).to_be_enabled()
                    composer.fill(reply)
                    page.get_by_role('button', name='回答并继续', exact=True).click()
            assert resumed.value.status in {200, 202}, resumed.value.text()
            resume_body = json.loads(resumed.value.request.post_data)
            assert resume_body['expected_run_version'] == value['run_version'], 'resume did not use recovered authoritative Run version'
            report['resume_request'] = resume_body
            value, resumed_snapshots = wait_outcome(page, context, base_url, run_id, timeout=timeout, progress=report['ui_progress'])
            report['snapshots'].extend(resumed_snapshots)
            report['reply'] = reply
        assert value['status'] == 'completed', f"real run did not complete: {value['status']} {value.get('diagnostic')}"
        expect(page.get_by_label('任务运行状态')).to_contain_text('已完成', timeout=30000)
        assert has_visible_active_progress(report['ui_progress']), 'no live planning/execution progress was visible in the browser'
        report['final_progress_text'] = verify_visible_progress(page, value)
        dependencies = sql_rows(root / 'runtime.db', 'SELECT d.task_id,d.depends_on_task_id FROM task_dependencies d JOIN tasks t ON t.task_id=d.task_id JOIN runs r ON r.active_plan_id=t.plan_id WHERE r.run_id=?', (run_id,))
        verify_step_order([task['task_id'] for task in value['tasks']], dependencies)
        report['task_dependencies'] = dependencies
        expect(composer).to_be_enabled(timeout=30000)
        final_ids = set(value.get('final_artifact_ids', []))
        assert final_ids, 'snapshot lacks authoritative final Artifact IDs'
        final = [item for item in value['output_artifacts'] if item['artifact_id'] in final_ids]
        maps = [item for item in final if item['type'] == 'MapLayer']
        files = [item for item in final if item['type'] == 'File']
        assert len(maps) == 1 and len(files) == 1, 'requested final map and GPKG File are missing or ambiguous'
        workspace = page.get_by_label('成果工作区', exact=True)
        expect(workspace).to_have_attribute('data-result-run-id', run_id)
        expect(workspace).to_have_attribute('data-artifact-id', maps[0]['artifact_id'])
        workspace.scroll_into_view_if_needed()
        table = workspace.get_by_role('table', name='成果属性表')
        expect(table).to_be_visible(timeout=30000)
        if case_id.startswith('housing'):
            columns = table.locator('thead th').all_text_contents()
            assert '房源ID' in columns, 'visible result table lost housing identity'
            expect(table.locator('tbody tr')).to_have_count(len(report['expected_housing_ids']))
            identity_index = columns.index('房源ID') - 1
            visible_ids = [row.locator('td').nth(identity_index).inner_text() for row in table.locator('tbody tr').all()]
            assert sorted(visible_ids) == report['expected_housing_ids'], 'visible housing table differs from fixture-derived business oracle'
            report['visible_housing_ids'] = visible_ids
        # Requires actual map SDK success. Missing keys/network failure are a failed gate.
        expect(workspace.get_by_role('button', name='全屏展开地图')).to_be_visible(timeout=45000)
        assert workspace.locator('.amap-container').count() > 0, 'actual map renderer did not mount'
        report['map_renderer_visible'] = True
        observed_map = workspace.locator('[data-map-render-evidence]')
        expect(observed_map).to_have_count(1, timeout=45000)
        render_evidence = json.loads(observed_map.get_attribute('data-map-render-evidence'))
        report['map_render_evidence'] = render_evidence
        observed_map.scroll_into_view_if_needed()
        page.screenshot(path=str(case_root / '03-final-map.png'), full_page=True)
        map_response = context.request.get(f"{base_url}/api/artifacts/{maps[0]['artifact_id']}/content", headers={'X-User-Id': page.evaluate("localStorage.getItem('gismind.user_id')")})
        assert map_response.status == 200
        map_content = map_response.json()
        write_json(case_root / 'map-content.json', map_content)
        workspace.locator(f"button[data-artifact-id='{files[0]['artifact_id']}']").click()
        expect(workspace).to_have_attribute('data-artifact-id', files[0]['artifact_id'])
        link = workspace.get_by_role('link', name='下载原始成果')
        expect(link).to_have_attribute('href', f"/api/artifacts/{files[0]['artifact_id']}/content")
        with page.expect_download(timeout=30000) as transfer:
            link.click()
        downloaded = case_root / 'final.gpkg'
        transfer.value.save_as(downloaded)
        assert transfer.value.failure() is None, 'browser download failed'
        report['download'] = verify_download(case_id, downloaded, seed=seed)
        assert report['download']['sha256'] == files[0]['content_hash'], 'download bytes do not match final artifact CAS identity'
        verify_map_matches_download(map_content, downloaded)
        verify_rendered_geometry(render_evidence, downloaded, artifact_id=maps[0]['artifact_id'])
        report['final_artifact_ids'] = sorted(final_ids)
        report['download_artifact_id'] = files[0]['artifact_id']
        report['map_artifact_id'] = maps[0]['artifact_id']
        attempts = sql_rows(root / 'runtime.db', 'SELECT * FROM planner_attempts WHERE run_id=? ORDER BY created_at', (run_id,))
        workers = sql_rows(root / 'runtime.db', 'SELECT t.task_key,t.tool_name,t.status,a.executor_identity,a.status AS attempt_status FROM tasks t JOIN plans p ON p.plan_id=t.plan_id JOIN task_attempts a ON a.task_id=t.task_id WHERE p.run_id=? ORDER BY a.started_at', (run_id,))
        provider = json.loads((root / 'provider.json').read_text(encoding='utf-8'))
        verify_provenance(attempts, workers, provider, server_pid=server_pid)
        report['planner_attempts'] = attempts
        report['worker_attempts'] = workers
        report['plans'] = sql_rows(root / 'runtime.db', 'SELECT * FROM plans WHERE run_id=? ORDER BY created_at', (run_id,))
        report['events'] = sql_rows(root / 'runtime.db', 'SELECT * FROM events WHERE run_id=? ORDER BY seq', (run_id,))
        assert len([item for item in requests if item['method'] == 'POST' and item['path'] == '/api/runs']) == 1, 'browser recovery submitted a duplicate Run'
        assert len([item for item in requests if item['method'] == 'POST' and item['path'].endswith('/resume')]) == (1 if 'reply' in CASES[case_id] else 0), 'browser sent duplicate or unsolicited resume commands'
        if report.get('recovery'):
            previous_events = report['recovery']['events_before']
            assert report['events'][:len(previous_events)] == previous_events, 'final execution changed pre-recovery durable events'
        assert any(item['path'] == f'/api/runs/{run_id}/events' and item['status'] == 200 and 'text/event-stream' in item['content_type'] for item in responses), 'browser did not establish actual SSE'
        assert not page_errors, page_errors
        page.screenshot(path=str(case_root / '04-download-selected.png'), full_page=True)
        # Fresh page recovery must select this completed run again from durable snapshots.
        page.reload(wait_until='networkidle')
        expect(page.get_by_label('成果工作区', exact=True)).to_have_attribute('data-artifact-id', maps[0]['artifact_id'], timeout=30000)
        report['recovered_progress_text'] = verify_visible_progress(page, value)
        page.screenshot(path=str(case_root / '05-recovered.png'), full_page=True)
        report['desktop_task_details'] = exercise_task_details(page, value, case_root, 'desktop')
        page.set_viewport_size({'width': 320, 'height': 900})
        page.get_by_role('button', name='对话', exact=True).click()
        expect(page.get_by_label('任务运行状态', exact=True)).to_be_visible()
        expect(composer).to_be_enabled()
        report['mobile_task_details'] = exercise_task_details(page, value, case_root, 'mobile-320')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), 'mobile workbench overflows horizontally'
        page.screenshot(path=str(case_root / '06-mobile-progress.png'), full_page=True)
        if case_id == 'housing_clarify':
            page.get_by_role('radio', name='LIGHT', exact=True).click()
            expect(page.get_by_role('radio', name='LIGHT', exact=True)).to_be_checked()
            page.screenshot(path=str(case_root / '07-mobile-light.png'), full_page=True)
        report['passed'] = True
    except Exception as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
        try:
            page.screenshot(path=str(case_root / 'failure.png'), full_page=True)
        except Exception:
            pass
    finally:
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        report['requests'], report['responses'], report['page_errors'] = requests, responses, page_errors
        write_json(case_root / 'result.json', report)
        context.tracing.stop(path=str(case_root / 'trace.zip'))
        context.close()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', action='store_true')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--preflight', action='store_true', help='start real servers and Chrome, upload one file; make zero Run/model requests')
    parser.add_argument('--cases', nargs='+', choices=list(CASES), default=list(BASELINE_CASES), help='default: original four baseline cases; seeded/recovery cases are opt-in')
    parser.add_argument('--seed', type=int, default=0, help='deterministic housing fixture seed (incremented per repeat)')
    parser.add_argument('--repeat', type=int, default=1, help='explicit repetition count 1..5; at most 20 total executions')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--max-cost-usd', type=float)
    parser.add_argument('--input-usd-per-million', type=float)
    parser.add_argument('--output-usd-per-million', type=float)
    parser.add_argument('--max-planner-calls', type=int, default=8)
    parser.add_argument('--timeout-seconds', type=float, default=240)
    parser.add_argument('--browser-channel', default='chrome' if os.name == 'nt' else None)
    args = parser.parse_args(argv)
    try:
        schedule = schedule_cases(args.cases, seed=args.seed, repeat=args.repeat)
    except ValueError as error:
        parser.error(str(error))
    if args.inventory:
        print(json.dumps({'suite': 'real_browser_runtime_v2', 'cases': CASES, 'default_cases': BASELINE_CASES,
                          'schedule': schedule, 'source_hashes': source_hashes(), 'live_calls': 0}, ensure_ascii=False, indent=2))
        return 0
    if not args.live and not args.preflight:
        parser.error('--live is required; this command can spend real provider quota')
    if args.live and args.preflight:
        parser.error('--live and --preflight are mutually exclusive')
    minimum_calls = sum(2 if 'reply' in CASES[item['case_id']] else 1 for item in schedule)
    if args.live and args.max_planner_calls < minimum_calls:
        parser.error(f'--max-planner-calls must cover at least {minimum_calls} first attempts for the selected schedule; repair attempts may need additional budget')
    if not args.output or any(value is None for value in (args.max_cost_usd, args.input_usd_per_million, args.output_usd_per_million)):
        parser.error('fresh --output, --max-cost-usd and both provider token prices are required')
    from tests.live.real_export_format_smoke import CostBudget, require_real_provider
    from app.config import settings
    CostBudget(max_usd=args.max_cost_usd, input_usd_per_million=args.input_usd_per_million,
               output_usd_per_million=args.output_usd_per_million, max_planner_calls=args.max_planner_calls)
    require_real_provider(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL, model=settings.LLM_MODEL)
    root = args.output.resolve()
    allowed = (BACKEND / '.gismind').resolve()
    if root == allowed or not root.is_relative_to(allowed):
        parser.error('--output must be a fresh dedicated directory inside backend/.gismind')
    if not (FRONTEND / 'node_modules/vite/bin/vite.js').is_file():
        parser.error('frontend dependencies are missing; run npm ci in frontend first')
    node = shutil.which('node')
    if not node:
        parser.error('Node.js is required for the real frontend server')
    from playwright.sync_api import sync_playwright
    root.mkdir(parents=True, exist_ok=False)
    manifest = source_hashes()
    configuration = {key: value for key, value in vars(args).items() if key not in {'output'}}
    configuration['output'] = str(root)
    configuration['schedule'] = schedule
    write_json(root / 'config.json', configuration)
    write_json(root / 'source-manifest.json', manifest)
    records = []
    process_report = {'lifecycle': {}, 'checks': []}
    report = {'suite': 'real_browser_runtime_v2', 'passed': False, 'server_transport': 'real_http_uvicorn',
              'worker_mode': 'normal_lifespan_autostart', 'network_stubs': False, 'source_hashes': manifest}
    try:
        with ExitStack() as stack:
            backend_port, frontend_port = free_port(), free_port()
            while frontend_port == backend_port:
                frontend_port = free_port()
            backend_url = f'http://127.0.0.1:{backend_port}'
            base_url = f'http://127.0.0.1:{frontend_port}'
            env = {**os.environ, 'GISMIND_LIVE_LLM': '1', 'GISMIND_BROWSER_CONFIG': str(root / 'config.json'),
                   'PYTHONUTF8': '1', 'APP_ENV': 'dev', 'GISMIND_VITE_API_TARGET': backend_url, 'VITE_API_BASE_URL': '/api'}
            server = stack.enter_context(owned_process([sys.executable, '-m', 'uvicorn', 'tests.browser.runtime_live_browser_server:create_live_app', '--factory', '--host', '127.0.0.1', '--port', str(backend_port)], BACKEND, env, root / 'backend.log', lifecycle=process_report['lifecycle'], name='backend'))
            report['health'] = wait_server(backend_url + '/api/health', server, worker=True)
            vite = stack.enter_context(owned_process([node, str(FRONTEND / 'node_modules/vite/bin/vite.js'), '--host', '127.0.0.1', '--port', str(frontend_port), '--strictPort'], FRONTEND, env, root / 'frontend.log', lifecycle=process_report['lifecycle'], name='frontend'))
            wait_server(base_url, vite)
            owned_servers = {'backend': server, 'frontend': vite}
            process_report['checks'].append(process_states(owned_servers, phase='ready'))
            write_json(root / 'processes.json', process_report)
            require_owned_servers(process_report['checks'][-1])
            report['launcher_pid'] = server.pid
            # Windows venv python.exe can be a redirector parent. The lifespan's
            # worker identity contains the actual API process PID.
            worker_id = report['health']['checks']['worker']['worker_id']
            assert worker_id.startswith('api-'), 'expected normal API lifespan worker identity'
            server_pid = int(worker_id.split('-')[1])
            report['server_pid'] = server_pid
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, **({'channel': args.browser_channel} if args.browser_channel else {}))
                try:
                    report['browser_version'] = browser.version
                    if args.preflight:
                        page = browser.new_page()
                        observed_runs = []
                        page.on('request', lambda request: observed_runs.append(request.url) if request.method == 'POST' and urlsplit(request.url).path == '/api/runs' else None)
                        page.goto(base_url, wait_until='networkidle')
                        from playwright.sync_api import expect
                        expect(page.get_by_role('textbox', name='分析需求')).to_be_enabled(timeout=30000)
                        name, fixture = fixture_uploads('buffer')[0]
                        with page.expect_response(lambda response: response.request.method == 'POST' and urlsplit(response.url).path == '/api/artifacts/uploads', timeout=60000) as uploaded:
                            page.locator('input[type=file]').set_input_files({'name': name, 'mimeType': 'application/geo+json', 'buffer': json.dumps(fixture, ensure_ascii=False).encode('utf-8')})
                        assert uploaded.value.status == 201, uploaded.value.text()
                        expect(page.get_by_text(name + ' · 就绪', exact=False)).to_be_visible()
                        page.screenshot(path=str(root / 'preflight.png'), full_page=True)
                        assert not observed_runs and not (root / 'provider.json').exists(), 'preflight unexpectedly created a Run or contacted the model'
                        report['preflight_passed'] = True
                        page.close()
                    for scheduled in ([] if args.preflight else schedule):
                        case_id = scheduled['case_id']
                        assert source_hashes() == manifest, 'sources changed; refusing further paid calls'
                        before = process_states(owned_servers, phase='before_case', case_id=scheduled['evidence_id'])
                        process_report['checks'].append(before)
                        write_json(root / 'processes.json', process_report)
                        require_owned_servers(before)
                        try:
                            case = run_case(browser, base_url, case_id, root, server_pid, args.timeout_seconds,
                                            seed=scheduled['seed'], evidence_id=scheduled['evidence_id'])
                        finally:
                            after = process_states(owned_servers, phase='after_case', case_id=scheduled['evidence_id'])
                            process_report['checks'].append(after)
                            write_json(root / 'processes.json', process_report)
                        case['owned_servers'] = {'before': before, 'after': after}
                        try:
                            require_owned_servers(after)
                        except RuntimeError as error:
                            case['passed'] = False
                            case['process_error'] = str(error)
                            case['error'] = case.get('error') or str(error)
                        write_json(root / scheduled['evidence_id'] / 'result.json', case)
                        records.append(case)
                        print(json.dumps({'case': scheduled['evidence_id'], 'seed': scheduled['seed'], 'passed': case['passed'], 'error': case.get('error')}, ensure_ascii=False), flush=True)
                        require_owned_servers(after)
                        if not case['passed']:
                            break  # Do not spend additional quota after a failed real gate.
                finally:
                    browser.close()
            process_report['checks'].append(process_states(owned_servers, phase='before_teardown'))
            write_json(root / 'processes.json', process_report)
            require_owned_servers(process_report['checks'][-1])
    except Exception as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
    for name, lifecycle in process_report['lifecycle'].items():
        state = lifecycle.get('before_cleanup', {})
        if state.get('alive') is False:
            message = f"owned {name} server exited before harness cleanup with exit code {state['exit_code']} (PID {state['pid']}, {state['observed_at']})"
            report.setdefault('process_error', message)
            report.setdefault('error', message)
    report['processes'] = process_report
    write_json(root / 'processes.json', process_report)
    report['source_unchanged'] = source_hashes() == manifest
    report['cases'] = [{key: case.get(key) for key in ('case_id', 'evidence_id', 'seed', 'passed', 'error', 'elapsed_seconds')} for case in records]
    report['scheduled_executions'] = len(schedule)
    report['executed_cases'] = len(records)
    report['passed_cases'] = sum(case['passed'] for case in records)
    report['first_failure'] = next((case for case in report['cases'] if not case['passed']), None)
    provider_path = root / 'provider.json'
    report['provider'] = json.loads(provider_path.read_text(encoding='utf-8')) if provider_path.exists() else {'responses': []}
    report['provider_http_calls_observed'] = sum(item.get('http_calls', 0) for item in report['provider']['responses'])
    report['passed'] = not report.get('error') and report['source_unchanged'] and len(records) == len(schedule) and all(case['passed'] for case in records) and report['provider_http_calls_observed'] > 0
    write_json(root / 'summary.json', report)
    # Both owned servers have stopped, so SQLite/CAS/log bytes are now frozen.
    seal_evidence(root)
    verify_evidence_seal(root)
    print(json.dumps({key: report[key] for key in ('suite', 'passed', 'source_unchanged', 'provider_http_calls_observed')}, ensure_ascii=False))
    return 0 if report['passed'] or (args.preflight and report.get('preflight_passed') and not report.get('error')) else 1


if __name__ == '__main__':
    raise SystemExit(main())
