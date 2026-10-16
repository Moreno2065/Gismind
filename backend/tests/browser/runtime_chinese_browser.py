"""Real Chrome + real DeepSeek + production worker, with a frozen Chinese corpus.

Inventory/preflight are offline-provider checks and never count toward the live gate.
Every paid invocation uses a fresh evidence directory and a shared durable budget.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import sys
import threading
import time
from urllib.parse import urlsplit

from tests.browser.runtime_live_browser import (
    BACKEND, FRONTEND, free_port, owned_process, wait_server, write_json,
    sql_rows, snapshot, process_states, require_owned_servers, seal_evidence, verify_evidence_seal,
    observe_live_progress, wait_outcome, has_visible_active_progress,
    verify_visible_progress, verify_rendered_geometry,
)
from tests.browser.runtime_live_browser_support import source_hashes, verify_provenance, verify_map_matches_download


def select_cases(cases, *, limit=2000, case_ids=None):
    indexed = {case.case_id: case for case in cases}
    if len(indexed) != len(cases):
        raise ValueError('duplicate corpus case ID')
    if case_ids:
        if len(set(case_ids)) != len(case_ids):
            raise ValueError('duplicate requested case ID')
        unknown = set(case_ids) - indexed.keys()
        if unknown:
            raise ValueError(f'unknown case IDs: {sorted(unknown)}')
        return [indexed[name] for name in case_ids]
    if not 1 <= limit <= 2000:
        raise ValueError('limit must be 1..2000')
    families = list(dict.fromkeys(case.family for case in cases))
    # Early batches cover business families, natural ambiguity and physical formats.
    # The permutation changes order only, never corpus membership or semantics.
    def priority(case):
        family = families.index(case.family)
        return ((case.paraphrase - family) % 10, (case.profile - family) % 20, family)
    return sorted(cases, key=priority)[:limit]


def full_gate(records, *, source_unchanged):
    return (source_unchanged and len(records) == 2000
            and len({row['case_id'] for row in records}) == 2000
            and all(row.get('passed') and row.get('real_provider_verified') for row in records))


def frontend_command(node, port, *, windows=None):
    # A narrow mitigation for native 0xC0000409 exits on Windows Insider:
    # nodejs/node#62260. Do not change Chrome or the application's JS semantics.
    windows = os.name == 'nt' if windows is None else windows
    return [node, *(['--no-maglev'] if windows else []),
            str(FRONTEND / 'node_modules/vite/bin/vite.js'), '--host', '127.0.0.1',
            '--port', str(port), '--strictPort']


def verify_clarification(value, terms):
    assert value.get('status') == 'awaiting_input', 'ambiguous request did not pause for clarification'
    assert not value.get('tasks') and not value.get('output_artifacts'), 'unresolved request already executed'
    question = (value.get('awaiting_input') or {}).get('message', '')
    assert isinstance(question, str), 'clarification has no visible question'
    assert terms and any(term in question for term in terms), 'clarification did not address the missing business parameter'
    if '面积' in terms:
        assert any(term in question for term in ('价格', '预算', '总价', '价位')), 'clarification omitted the budget'
        assert any(term in question for term in ('面积', '平方', '多大', '宽敞')), 'clarification omitted the area'


def verify_table_rows(columns, rows, features):
    def display(value):
        if value is None:
            return '—'
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    fields = columns[1:]
    required = set().union(*(set(feature.get('properties', {})) for feature in features))
    assert set(fields) == required, 'visible table lost or invented fields'
    expected = Counter(tuple(display(feature['properties'].get(field)) for field in fields) for feature in features)
    assert Counter(map(tuple, rows)) == expected, 'visible result table differs from final spatial payload'


def _provider(root):
    path = root / 'provider.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'responses': []}


def capture_request(request):
    path = urlsplit(request.url).path
    result = {'method': request.method, 'path': path}
    # Multipart is binary (SHP, GPKG, TIFF), not a UTF-8 JSON request body.
    if request.method == 'POST' and (path == '/api/runs' or path.startswith('/api/runs/')):
        result['body'] = request.post_data
    return result


def verify_run_session(body, session_id):
    assert body.get('session_id') == session_id, 'case submitted into a recovered history session'


def track_accepted_run(response, report):
    # A 202 may already be spending quota, even if its response is malformed.
    report['cleanup_incomplete'] = True
    run_id = response.json().get('run_id')
    assert isinstance(run_id, str) and run_id.strip(), 'accepted Run has no usable ID'
    report['run_id'] = run_id
    report.pop('cleanup_incomplete')
    return run_id


def open_fresh_session(page, base_url):
    from playwright.sync_api import expect

    # A recovered awaiting-input Run has a live SSE stream: networkidle is not
    # page readiness. Use the real navigation controls to isolate every case.
    page.goto(base_url, wait_until='domcontentloaded')
    expect(page.get_by_role('textbox', name='分析需求')).to_be_visible(timeout=30000)
    toggle = page.get_by_role('button', name=re.compile(r'^(展开|收起)侧栏$'))
    expect(toggle).to_be_visible(timeout=30000)
    if toggle.get_attribute('aria-expanded') != 'true':
        toggle.click()
    button = page.get_by_role('button', name='+ 新建会话', exact=True)
    expect(button).to_be_enabled(timeout=30000)
    with page.expect_response(lambda response: response.request.method == 'POST' and urlsplit(response.url).path == '/api/sessions', timeout=30000) as created:
        button.click()
    assert created.value.status == 201, created.value.text()
    session_id = created.value.json()['id']
    page.wait_for_function("id => localStorage.getItem('gismind.active_session') === id", arg=session_id)
    expect(page.get_by_role('textbox', name='分析需求')).to_be_enabled(timeout=30000)
    return session_id


def _facts(root, run_id):
    database = root / 'runtime.db'
    return {
        'planner_attempts': sql_rows(database, 'SELECT * FROM planner_attempts WHERE run_id=? ORDER BY created_at', (run_id,)),
        'worker_attempts': sql_rows(database, 'SELECT t.task_key,t.tool_name,t.status,a.executor_identity,a.status AS attempt_status FROM tasks t JOIN plans p ON p.plan_id=t.plan_id JOIN task_attempts a ON a.task_id=t.task_id WHERE p.run_id=? ORDER BY a.started_at', (run_id,)),
        'plans': sql_rows(database, 'SELECT * FROM plans WHERE run_id=? ORDER BY created_at', (run_id,)),
        'events': sql_rows(database, 'SELECT * FROM events WHERE run_id=? ORDER BY seq', (run_id,)),
        'dependencies': sql_rows(database, 'SELECT d.* FROM task_dependencies d JOIN tasks t ON t.task_id=d.task_id JOIN plans p ON p.plan_id=t.plan_id WHERE p.run_id=?', (run_id,)),
    }


def run_case(browser, base_url, case, root, server_pid, timeout, *, preflight=False):
    from playwright.sync_api import expect
    from tests.browser.runtime_chinese_fixtures import build_uploads, verify_case

    case_root = root / case.case_id
    case_root.mkdir()
    report = {**asdict(case), 'passed': False, 'real_provider_verified': False,
              'uploads': [], 'ui_progress': [], 'preflight': preflight}
    started = time.monotonic()
    context = browser.new_context(viewport={'width': 1600, 'height': 1040}, accept_downloads=True, reduced_motion='reduce')
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    page = context.new_page()
    requests, responses, page_errors = [], [], []
    page.on('request', lambda req: requests.append(capture_request(req)))
    page.on('response', lambda res: responses.append({'path': urlsplit(res.url).path, 'status': res.status,
        'content_type': res.headers.get('content-type', '')}))
    page.on('pageerror', lambda error: page_errors.append(str(error)))
    run_id = None
    try:
        report['session_id'] = open_fresh_session(page, base_url)
        composer = page.get_by_role('textbox', name='分析需求')
        expect(composer).to_be_enabled(timeout=30000)
        uploads = build_uploads(case, case_root)
        for upload in uploads:
            name, data = upload.filename, upload.content_bytes
            (case_root / name).write_bytes(data)
            with page.expect_response(lambda res: res.request.method == 'POST' and urlsplit(res.url).path == '/api/artifacts/uploads', timeout=60000) as pending:
                page.locator('input[type=file]').set_input_files({'name': name, 'mimeType': upload.mime_type, 'buffer': data})
            response = pending.value
            assert response.status == 201, f'upload {name}: HTTP {response.status} {response.text()}'
            metadata = response.json()
            assert metadata['content_hash'] == hashlib.sha256(data).hexdigest(), 'upload bytes changed'
            report['uploads'].append({'physical_format': upload.physical_format, **metadata})
            expect(page.get_by_text(name + ' · 就绪', exact=False)).to_be_visible()
        page.screenshot(path=str(case_root / 'uploaded.png'), full_page=True)
        if preflight:
            assert not any(req['path'] == '/api/runs' for req in requests), 'preflight created a Run'
            report['preflight_passed'] = True
            return report
        observe_live_progress(page)
        composer.fill(case.prompt)
        with page.expect_response(lambda res: res.request.method == 'POST' and urlsplit(res.url).path == '/api/runs', timeout=30000) as created:
            page.get_by_role('button', name='发送', exact=True).click()
        assert created.value.status == 202, created.value.text()
        run_id = track_accepted_run(created.value, report)
        verify_run_session(json.loads(created.value.request.post_data), report['session_id'])
        value, snapshots = wait_outcome(page, context, base_url, run_id, timeout=timeout, progress=report['ui_progress'])
        report['snapshots'] = snapshots
        if case.clarification_reply:
            verify_clarification(value, case.clarification_terms)
            expect(page.get_by_label('任务运行状态')).to_contain_text('等待补充信息', timeout=15000)
            page.screenshot(path=str(case_root / 'clarification.png'), full_page=True)
            report['clarification'] = value['awaiting_input']
            form = page.locator('.runtime-input-form')
            with page.expect_response(lambda res: res.request.method == 'POST' and urlsplit(res.url).path.endswith('/resume'), timeout=30000) as resumed:
                if form.count():
                    controls = form.get_by_role('textbox')
                    assert controls.count() == 1, 'unexpected clarification form needs explicit scenario coverage'
                    controls.fill(case.clarification_reply)
                    form.get_by_role('button', name='提交补充信息').click()
                else:
                    expect(composer).to_be_enabled()
                    composer.fill(case.clarification_reply)
                    page.get_by_role('button', name='回答并继续', exact=True).click()
            assert resumed.value.status in {200, 202}, resumed.value.text()
            assert json.loads(resumed.value.request.post_data)['expected_run_version'] == value['run_version']
            value, resumed_snapshots = wait_outcome(page, context, base_url, run_id, timeout=timeout, progress=report['ui_progress'])
            report['snapshots'].extend(resumed_snapshots)
        report['snapshot'] = value
        assert value['status'] == 'completed', f"run {value['status']}: {value.get('diagnostic') or value.get('awaiting_input')}"
        expect(page.get_by_label('任务运行状态')).to_contain_text('已完成', timeout=30000)
        assert has_visible_active_progress(report['ui_progress']), 'no active progress visible in browser'
        report['visible_progress'] = verify_visible_progress(page, value)
        expect(composer).to_be_enabled(timeout=15000)
        final_ids = set(value.get('final_artifact_ids', []))
        final = [item for item in value['output_artifacts'] if item['artifact_id'] in final_ids]
        assert final and len(final) == len(final_ids), 'no authoritative final results'
        workspace = page.get_by_label('成果工作区', exact=True)
        expect(workspace).to_have_attribute('data-result-run-id', run_id)
        downloaded = []
        map_observations = []
        for artifact in final:
            artifact_id = artifact['artifact_id']
            workspace.locator(f'button[data-artifact-id="{artifact_id}"]').click()
            expect(workspace).to_have_attribute('data-artifact-id', artifact_id)
            link = workspace.get_by_role('link', name='下载原始成果')
            expect(link).to_have_attribute('href', f'/api/artifacts/{artifact_id}/content')
            with page.expect_download(timeout=30000) as transfer:
                link.click()
            suffix = '.tif' if artifact['type'] == 'Raster' else '.gpkg' if artifact['type'] == 'File' else '.json'
            path = case_root / f'{artifact_id}{suffix}'
            transfer.value.save_as(path)
            assert transfer.value.failure() is None, 'browser download failed'
            assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact['content_hash'], 'download/CAS hash mismatch'
            downloaded.append({'artifact': artifact, 'path': path})
            if artifact['type'] == 'MapLayer':
                content = json.loads(path.read_bytes())
                features = [feature for layer in content.get('layers', []) for feature in layer.get('features', [])]
                table = workspace.get_by_role('table', name='成果属性表')
                expect(table).to_be_visible(timeout=30000)
                expect(table.locator('tbody tr')).to_have_count(len(features))
                verify_table_rows(table.locator('thead th').all_text_contents(),
                    [row.locator('td').all_text_contents() for row in table.locator('tbody tr').all()], features)
                expect(workspace.get_by_role('button', name='全屏展开地图')).to_be_visible(timeout=45000)
                assert workspace.locator('.amap-container').count(), 'actual map SDK not mounted'
                node = workspace.locator('[data-map-render-evidence]')
                expect(node).to_have_count(1, timeout=45000)
                map_observations.append({'artifact_id': artifact_id, 'content': content,
                    'rendered': json.loads(node.get_attribute('data-map-render-evidence'))})
            if artifact['type'] == 'Raster':
                expect(workspace).to_contain_text('尚无浏览器栅格预览', timeout=15000)
            page.screenshot(path=str(case_root / f'visible-{artifact_id}.png'), full_page=True)
        report.update(_facts(root, run_id))
        report['oracle'] = verify_case(case, uploads, value, downloaded, case_root)
        files = [item for item in downloaded if item['artifact']['type'] == 'File']
        for observed in map_observations:
            assert len(files) == 1, 'one final vector download is required alongside the map'
            verify_map_matches_download(observed['content'], files[0]['path'])
            verify_rendered_geometry(observed['rendered'], files[0]['path'], artifact_id=observed['artifact_id'])
        report['map_observations'] = map_observations
        verify_provenance(report['planner_attempts'], report['worker_attempts'], _provider(root), server_pid=server_pid)
        report['real_provider_verified'] = True
        assert sum(req['method'] == 'POST' and req['path'] == '/api/runs' for req in requests) == 1
        assert sum(req['method'] == 'POST' and req['path'].endswith('/resume') for req in requests) == bool(case.clarification_reply)
        assert any(res['path'] == f'/api/runs/{run_id}/events' and res['status'] == 200 and 'text/event-stream' in res['content_type'] for res in responses), 'actual browser SSE absent'
        assert not page_errors, page_errors
        report['passed'] = True
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
        if run_id:
            try:
                report.update(_facts(root, run_id))
                state = snapshot(context, base_url, run_id, {'X-User-Id': page.evaluate("localStorage.getItem('gismind.user_id')")})
                report['failure_snapshot'] = state
                if state['status'] not in {'completed', 'failed', 'cancelled', 'awaiting_input'}:
                    # Stop this exact visible Run before another case can spend quota.
                    stop_button = page.get_by_role('button', name='停止', exact=True)
                    expect(stop_button).to_be_enabled(timeout=10000)
                    with page.expect_response(lambda res: res.request.method == 'POST' and urlsplit(res.url).path == f'/api/runs/{run_id}/cancel', timeout=15000) as cancelled:
                        stop_button.click()
                    assert cancelled.value.status in {200, 202}, 'failed Run cancellation rejected'
                    final_state, _ = wait_outcome(page, context, base_url, run_id, timeout=20)
                    report['failure_cleanup'] = {'status': final_state['status'], 'run_id': run_id}
            except Exception as fact_error:
                report['fact_capture_error'] = str(fact_error)
                report['cleanup_incomplete'] = True
        try:
            page.screenshot(path=str(case_root / 'failure.png'), full_page=True)
        except Exception:
            pass
    finally:
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        report.update(requests=requests, responses=responses, page_errors=page_errors)
        try:
            context.tracing.stop(path=str(case_root / 'trace.zip'))
        finally:
            context.close()
            write_json(case_root / 'result.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--inventory', action='store_true')
    mode.add_argument('--preflight', action='store_true')
    mode.add_argument('--live', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--budget-db', type=Path)
    parser.add_argument('--max-cost-usd', type=float, default=10)
    parser.add_argument('--input-usd-per-million', type=float, default=.3)
    parser.add_argument('--output-usd-per-million', type=float, default=1.2)
    parser.add_argument('--cache-hit-usd-per-million', type=float, default=.006)
    parser.add_argument('--max-planner-calls', type=int, default=6000)
    parser.add_argument('--concurrency', type=int, choices=(1, 2, 3, 4), default=2)
    parser.add_argument('--limit', type=int, default=2000)
    parser.add_argument('--case-ids', nargs='+')
    parser.add_argument('--max-failures', type=int, default=5)
    parser.add_argument('--timeout-seconds', type=float, default=240)
    parser.add_argument('--browser-channel', default='chrome' if os.name == 'nt' else None)
    args = parser.parse_args(argv)
    from tests.browser.runtime_chinese_corpus import load_cases, corpus_digest, CORPUS_SHA256
    all_cases = load_cases()
    if len(all_cases) != 2000 or corpus_digest(all_cases) != CORPUS_SHA256:
        parser.error('frozen corpus integrity check failed before provider setup')
    try:
        cases = select_cases(all_cases, limit=args.limit, case_ids=args.case_ids)
    except ValueError as error:
        parser.error(str(error))
    if args.inventory:
        print(json.dumps({'total': len(all_cases), 'scheduled': len(cases), 'corpus_sha256': corpus_digest(),
            'families': dict(Counter(case.family for case in all_cases)),
            'formats': dict(Counter(case.upload_format for case in all_cases)), 'provider_calls': 0}, ensure_ascii=False))
        return 0
    if not args.output or not args.budget_db or args.max_failures < 1:
        parser.error('fresh --output, shared --budget-db, positive --max-failures are required')
    allowed = (BACKEND / '.gismind').resolve()
    root, budget_path = args.output.resolve(), args.budget_db.resolve()
    if root == allowed or not root.is_relative_to(allowed) or budget_path == allowed or not budget_path.is_relative_to(allowed) or budget_path.is_relative_to(root):
        parser.error('output and shared budget must be separate paths under backend/.gismind')
    if not (FRONTEND / 'node_modules/vite/bin/vite.js').is_file() or not shutil.which('node'):
        parser.error('Node.js and frontend dependencies are required')
    from tests.live.real_export_format_smoke import require_real_provider
    from tests.browser.runtime_browser_budget import DurableCostBudget
    from app.config import settings
    require_real_provider(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL, model=settings.LLM_MODEL)
    rates = {key: getattr(args, key) for key in ('input_usd_per_million', 'output_usd_per_million', 'cache_hit_usd_per_million', 'max_planner_calls')}
    budget = DurableCostBudget(budget_path, max_usd=args.max_cost_usd, **rates)
    root.mkdir(parents=True, exist_ok=False)
    manifest = source_hashes()
    absolute_manifest = {str(BACKEND.parent / name): digest for name, digest in manifest.items()}
    write_json(root / 'source-manifest.json', manifest)
    write_json(root / 'corpus.json', {'sha256': corpus_digest(), 'cases': [asdict(case) for case in all_cases]})
    write_json(root / 'schedule.json', [case.case_id for case in cases])
    pending = queue.Queue()
    for case in cases:
        pending.put(case)
    records, shard_reports = [], []
    lock, stop = threading.Lock(), threading.Event()
    started = time.monotonic()

    def shard(index):
        from playwright.sync_api import sync_playwright
        shard_root = root / f'shard-{index + 1}'
        shard_root.mkdir()
        process_report = {'lifecycle': {}, 'checks': []}
        shard_report = {'shard': index + 1, 'processes': process_report}
        config = {'output': str(shard_root), 'budget_db': str(budget_path), 'max_cost_usd': args.max_cost_usd,
                  **rates, 'timeout_seconds': args.timeout_seconds, 'source_manifest': absolute_manifest}
        write_json(shard_root / 'config.json', config)
        try:
            with ExitStack() as stack:
                backend_port, frontend_port = free_port(), free_port()
                while frontend_port == backend_port:
                    frontend_port = free_port()
                backend_url, base_url = f'http://127.0.0.1:{backend_port}', f'http://127.0.0.1:{frontend_port}'
                env = {**os.environ, 'GISMIND_LIVE_LLM': '1', 'GISMIND_BROWSER_CONFIG': str(shard_root / 'config.json'),
                       'PYTHONUTF8': '1', 'APP_ENV': 'dev', 'GISMIND_VITE_API_TARGET': backend_url, 'VITE_API_BASE_URL': '/api'}
                server = stack.enter_context(owned_process([sys.executable, '-m', 'uvicorn', 'tests.browser.runtime_chinese_browser_server:create_app', '--factory', '--host', '127.0.0.1', '--port', str(backend_port)], BACKEND, env, shard_root / 'backend.log', lifecycle=process_report['lifecycle'], name='backend'))
                health = wait_server(backend_url + '/api/health', server, worker=True)
                vite = stack.enter_context(owned_process(frontend_command(shutil.which('node'), frontend_port), FRONTEND, env, shard_root / 'frontend.log', lifecycle=process_report['lifecycle'], name='frontend'))
                wait_server(base_url, vite)
                worker_id = health['checks']['worker']['worker_id']
                assert worker_id.startswith('api-')
                server_pid = int(worker_id.split('-')[1])
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True, **({'channel': args.browser_channel} if args.browser_channel else {}))
                    shard_report['browser_version'] = browser.version
                    try:
                        while not stop.is_set():
                            try:
                                case = pending.get_nowait()
                            except queue.Empty:
                                break
                            assert source_hashes() == manifest, 'source drift: no further paid cases allowed'
                            before = process_states({'backend': server, 'frontend': vite}, phase='before_case', case_id=case.case_id)
                            require_owned_servers(before)
                            process_report['checks'].append(before)
                            write_json(shard_root / 'processes.json', process_report)
                            result = run_case(browser, base_url, case, shard_root, server_pid, args.timeout_seconds, preflight=args.preflight)
                            after = process_states({'backend': server, 'frontend': vite}, phase='after_case', case_id=case.case_id)
                            process_report['checks'].append(after)
                            write_json(shard_root / 'processes.json', process_report)
                            try:
                                require_owned_servers(after)
                            except RuntimeError as error:
                                result['passed'], result['error'] = False, str(error)
                                stop.set()
                            result['evidence_path'] = str(shard_root / case.case_id)
                            # Append/fync before scheduling another paid case.
                            with lock:
                                compact = {key: result.get(key) for key in ('case_id', 'family', 'upload_format', 'passed', 'real_provider_verified', 'preflight_passed', 'error', 'elapsed_seconds', 'run_id', 'evidence_path')}
                                records.append(compact)
                                with (root / 'results.jsonl').open('a', encoding='utf-8') as stream:
                                    stream.write(json.dumps(compact, ensure_ascii=False) + '\n')
                                    stream.flush()
                                    os.fsync(stream.fileno())
                                counts = {'completed': len(records), 'scheduled': len(cases), 'passed': sum(row['passed'] for row in records),
                                          'budget': budget.summary(), 'last': compact}
                                write_json(root / 'progress.json', counts)
                                print(json.dumps(counts, ensure_ascii=False), flush=True)
                                failures = sum(not (row['preflight_passed'] if args.preflight else row['passed']) for row in records)
                                if failures >= args.max_failures or result.get('cleanup_incomplete'):
                                    stop.set()
                            pending.task_done()
                    finally:
                        browser.close()
                require_owned_servers(process_states({'backend': server, 'frontend': vite}, phase='before_teardown'))
        except Exception as error:
            shard_report['error'] = f'{type(error).__name__}: {error}'
            stop.set()
        finally:
            write_json(shard_root / 'processes.json', process_report)
            shard_report['provider'] = _provider(shard_root)
            with lock:
                shard_reports.append(shard_report)

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        list(executor.map(shard, range(args.concurrency)))
    unchanged = source_hashes() == manifest
    report = {'suite': 'real-chinese-browser-2000-v1', 'network_stubs': False, 'source_unchanged': unchanged,
              'corpus_sha256': corpus_digest(), 'scheduled': len(cases), 'executed': len(records),
              'passed_cases': sum(row['passed'] for row in records), 'cases': records, 'shards': shard_reports,
              'budget': budget.summary(), 'elapsed_seconds': time.monotonic() - started,
              'full_2000_gate': full_gate(records, source_unchanged=unchanged) and not any(row.get('error') for row in shard_reports)}
    report['passed'] = (unchanged and len(records) == len(cases) and not any(row.get('error') for row in shard_reports)
                        and all(row['preflight_passed'] if args.preflight else row['passed'] for row in records))
    if args.preflight:
        assert not any(row['provider']['responses'] for row in shard_reports), 'preflight made provider requests'
    write_json(root / 'summary.json', report)
    seal_evidence(root)
    verify_evidence_seal(root)
    print(json.dumps({key: report[key] for key in ('passed', 'executed', 'passed_cases', 'full_2000_gate', 'budget')}, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
