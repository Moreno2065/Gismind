"""Replay new-run saved SDK geometry and causal PNGs against fresh physical1800.

No browser/network/model calls; no large summary parsing or older pilot data.
The observer's global terminal native-SDK/cancellation gate is a separate check.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time

import numpy as np
from PIL import Image

TOOL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOL))
from sequence_review import RETRYABLE, strict_equal, validate_sequence
from physical_geometry import require, sha, load

PHASES = ['hidden-before', 'shown-first', 'hidden-after', 'shown-again']
SHOWN = [False, True, False, True]


def read_image(path, mode='RGB'):
    with Image.open(path) as image:
        return np.asarray(image.convert(mode), dtype=np.uint8)


class Reviewer:
    def __init__(self, root, original, helper):
        self.root, self.original, self.h = root, original, helper
        self.counts, self.metrics = Counter(), {}
        self.maximum_error = 0.0

    def snapshot(self, snapshot, initial, fresh, recorded, artifact_id, target, shown):
        h = self.h
        h.require_fixed_view(h.view_signature(initial), snapshot)
        require(snapshot['evidence']['sourceArtifactId'] == artifact_id, 'Snapshot Artifact differs')
        require(h._geometry_signature(snapshot) == h._geometry_signature(initial), 'SDK geometry/style/identity changed')
        require(snapshot['membership']['matched'] is True and snapshot['membership']['allCount'] == len(fresh), 'SDK membership differs')
        overlays = snapshot['overlays']
        require([v['overlay_index'] for v in overlays] == list(range(len(fresh))), 'Incomplete SDK overlay indexes')
        physical_oracle = h.verify_sdk_geometry(fresh, overlays, tolerance_degrees=2e-6)
        oracle = h.verify_sdk_geometry(recorded, overlays, tolerance_degrees=2e-6)
        self.maximum_error = max(self.maximum_error, physical_oracle['max_error_degrees'])
        for overlay in overlays:
            h.validate_overlay_style(overlay)
            require(h.normalize_sdk_overlay(overlay)['coordinates'] == overlay['coordinates'], 'Raw getPosition/getPath and normalized geometry differ')
            kind = overlay['kind']
            self.counts['getPosition' if kind == 'Point' else 'getPath'] += 1
            self.counts['SDK_positions'] += 1 if kind == 'Point' else sum(len(r) for r in overlay['coordinates'])
            if overlay['visibility_observable']:
                require(overlay['visible'] is (shown if target is None or target == overlay['overlay_index'] else False), 'Actual visibility/isolation differs')
            else:
                require(overlay['visible'] is None, 'Unobservable visibility must be null')
        self.counts['SDK_snapshots'] += 1
        return oracle

    def diagnostic(self, diag, target, shown, budget):
        require(diag['passed'] is True and diag['deadline_extended'] is False, 'Capture failed or deadline extended')
        require(diag['overlay_index'] == target and diag['shown'] is shown, 'Wrong capture target')
        require(diag['deadline_monotonic'] == budget['deadline_monotonic'] and diag['deadline_remaining_seconds'] > 0, 'Capture exceeded fixed budget')
        require(diag['stable_consecutive_comparisons'] >= 2 and diag['screenshot_return_count'] >= 3, 'Missing stable screenshot observations')
        require(diag['last_global_drift'] <= .002, 'Screenshot unstable')
        require(all(v['status'] == 'returned' and v['remaining_before_seconds'] > 0 and v['remaining_after_seconds'] > 0 for v in diag['operations']), 'Capture operation failed/exhausted')
        self.counts['capture_diagnostics'] += 1

    def metrics_add(self, value):
        for key, number in value.items():
            if type(number) in (int, float):
                extrema = self.metrics.setdefault(key, {'min': number, 'max': number})
                extrema['min'], extrema['max'] = min(extrema['min'], number), max(extrema['max'], number)

    def case(self, path, physical):
        h = self.h
        data = h.strict_read_json(path)
        require(data['passed'] is True and 'error' not in data and 'case_record_error' not in data, 'Observer case is not passed')
        require(path.parent.name == data['case_id'] and path.parent.parent.name == data['shard'], 'Wrong case path')
        for key in ('case_id', 'shard', 'run_id', 'session_id', 'map_artifact_id', 'gpkg_artifact_id', 'gpkg_sha256', 'original_result_sha256'):
            require(data[key] == physical[key], 'Physical/observation identity differs: ' + key)
        require(data['map_content_sha256'] == physical['map_sha256'], 'Map content identity differs')
        gpkg = self.original / data['shard'] / data['case_id'] / (data['gpkg_artifact_id'] + '.gpkg')
        require(sha(gpkg) == physical['gpkg_sha256'], 'GPKG no longer matches fresh physical review')
        fresh, recorded = physical['features'], data['independent_expected_gcj02_features']
        h.verify_sdk_geometry(fresh, [{'kind': f['geometry']['type'], 'coordinates': f['geometry']['coordinates']} for f in recorded], tolerance_degrees=1e-10)
        require(data['sdk_resource_native_identity_verified'] is True, 'Observer native SDK gate failed')
        require(data['historical_session_readonly_projection_verified'] is True and not data['forbidden_requests'], 'Readonly/network gate failed')
        require(not h.gate_task_errors(data['background_task_errors']), 'Blocking background errors')
        require(h.projection_observations_verified(data['historical_session_projection_responses'], data['session_id']), 'Saved session projection failed')
        initial, budget = data['initial_sdk_snapshot'], data['paint_work_budget']
        snapshot = lambda value, target=None, shown=True: self.snapshot(value, initial, fresh, recorded, data['map_artifact_id'], target, shown)
        initial_oracle = snapshot(initial)
        require(strict_equal(initial_oracle, data['initial_geometry_oracle']), 'Initial geometry statistics differ')
        require(budget['timeout_seconds'] == 30 and budget['deadline_extended'] is False and budget['target_overlay_count'] == len(fresh), 'Paint budget differs')
        require(budget['minimum_frame_phases'] == 1 + 4*len(fresh) + 4 and budget['minimum_screenshot_count'] == (1 + 4*len(fresh) + 4)*3, 'Paint workload cardinality differs')
        require(data['initial_capture_paint_readiness'] == 'not_yet_proved', 'Initial PNG falsely labeled proved')
        initial_png = path.parent / 'initial-before-paint-readiness.png'
        shape = read_image(initial_png).shape
        self.diagnostic(h.strict_read_json(path.parent / 'initial-frame-diagnostics/diagnostic.json'), None, True, budget)
        causal_paths, mask_paths = set(), set()

        def capture(frame, target, shown):
            fp = h.checked_output_path(self.root, frame['capture_path'])
            require(fp.is_relative_to(path.parent), 'PNG belongs to another case')
            require(sha(fp) == frame['capture_sha256'], 'PNG does not match recorded capture')
            causal_paths.add(fp)
            self.diagnostic(frame['capture_diagnostics'], target, shown, budget)
            require(frame['stable_consecutive_comparisons'] >= 2, 'Frame not stable')
            sidecar = fp.parent / (fp.stem + '-diagnostics') / 'diagnostic.json'
            require(strict_equal(h.strict_read_json(sidecar), frame['capture_diagnostics']), 'Diagnostic sidecar differs')
            snapshot(frame['snapshot'], target, shown)
            image = read_image(fp)
            require(image.shape == shape, 'Capture layout differs')
            return image

        def images_for(frames, target):
            require([f['phase'] for f in frames] == PHASES, 'Four phase order differs')
            return [capture(f, target, shown) for f, shown in zip(frames, SHOWN)]

        final = data['final_all_phases']
        final_images = images_for(final, None)
        validate_sequence({'frames': final}, h.strict_read_json(path.parent / 'sequence.json'), budget, expected_kind='final')
        assignment = {p['expected_index']: p['observed_index'] for p in initial_oracle['pairs']}
        require(set(assignment) == set(range(len(fresh))) and len(set(assignment.values())) == len(fresh), 'Feature/SDK assignment is not 1:1')
        require(len(data['overlay_observations']) == len(fresh), 'Overlay observation cardinality differs')
        require(len(data['independently_projected_expected_pixels']) == len(fresh), 'Expected projection cardinality differs')
        seen, masks = set(), {}
        for target in data['overlay_observations']:
            index, overlay = target['expected_index'], target['overlay_index']
            require(index not in seen and assignment[index] == overlay, 'Repeated/mismatched target assignment')
            seen.add(index)
            require(target['passed'] is True and target['paint_readiness'] == 'ready' and 'error' not in target, 'Target not ready')
            require(strict_equal(target['independent_geometry'], recorded[index]), 'Target physical geometry differs')
            projected = target['expected_projected_pixels']
            require(projected == data['independently_projected_expected_pixels'][index] and projected['kind'] == recorded[index]['geometry']['type'], 'Projection not bound to expected feature')
            mask = h.build_masks(projected['kind'], projected['coordinates'], shape[:2])
            require(mask['recipe'] == target['mask_recipe'], 'Mask recipe differs')
            masks[index] = mask
            overlay_dir = path.parent / f'overlay-{overlay:02d}'
            require(strict_equal(h.strict_read_json(overlay_dir / 'observation.json'), target), 'Overlay sidecar differs')
            for name in ('core_mask', 'contour_mask', 'allowed_mask'):
                if mask[name] is not None:
                    fp = overlay_dir / (name + '.png')
                    require(np.array_equal(read_image(fp, 'L') != 0, mask[name]), 'Independent projected mask PNG differs')
                    mask_paths.add(fp)
            attempts = target['measurements']
            require(bool(attempts), 'No target measurement')
            for number, measurement in enumerate(attempts):
                images = images_for(measurement['frames'], overlay)
                directory = overlay_dir / f'measurement-{number:03d}'
                require(strict_equal(h.strict_read_json(directory / 'measurement.json'), measurement), 'Measurement sidecar differs')
                validate_sequence(measurement, h.strict_read_json(directory / 'sequence.json'), budget, expected_kind='overlay')
                require(measurement['deadline_remaining_seconds'] > 0, 'Measurement exceeded paint budget')
                try:
                    statistics = h.verify_visible_paint(*images, core_mask=mask['core_mask'], contour_mask=mask['contour_mask'], allowed_mask=mask['allowed_mask'])
                except ValueError as error:
                    require(number < len(attempts)-1 and measurement['paint_readiness'] == 'not_ready', 'Rejected terminal paint measurement')
                    require('statistics' not in measurement and measurement['error'] == h.safe_error(error) and any(prefix in str(error) for prefix in RETRYABLE), 'Retained paint rejection differs')
                    self.counts['retained_rejected_measurements'] += 1
                else:
                    require(number == len(attempts)-1 and measurement['paint_readiness'] == 'ready' and 'error' not in measurement, 'Accepted measurement is not unique last-ready')
                    require(strict_equal(statistics, measurement['statistics']) and strict_equal(statistics, target['statistics']), 'Target paint statistics differ')
                    self.metrics_add(statistics)
                    self.counts['accepted_target_measurements'] += 1
                self.counts['target_measurements'] += 1
        require(seen == set(range(len(fresh))), 'Unreviewed feature')
        union = np.logical_or.reduce([masks[i]['allowed_mask'] for i in range(len(fresh))])
        require(len(data['final_all_shown_causal_paint_oracles']) == len(fresh), 'Missing final per-feature paint')
        for index, saved in enumerate(data['final_all_shown_causal_paint_oracles']):
            mask = masks[index]
            result = h.verify_visible_paint(*final_images, core_mask=mask['core_mask'], contour_mask=mask['contour_mask'], allowed_mask=union)
            require(strict_equal(result, saved), 'Final composition paint statistics differ')
            self.metrics_add(result)
            self.counts['final_per_feature_oracles'] += 1
        drift = np.max(np.abs(final_images[1].astype(np.int16)-final_images[3].astype(np.int16)), axis=2) >= 3
        require(float(np.mean(drift)) == data['final_all_shown_global_drift'] <= .002, 'Final global drift differs')
        require(float(np.mean(drift[union])) == data['final_all_shown_local_drift'] <= .002, 'Final local drift differs')
        require((path.parent / 'final-all-shown.png').read_bytes() == h.checked_output_path(self.root, final[-1]['capture_path']).read_bytes(), 'Final restored PNG differs')
        for kind in ('final', 'cleanup'):
            require(strict_equal(snapshot(data[kind + '_sdk_snapshot']), data[kind + '_geometry_oracle']), 'Final/cleanup full geometry differs')
        cleanup = data['cleanup_diagnostics']
        require(cleanup['passed'] is True and cleanup['restored_all_shown'] is True and cleanup['deadline_extended'] is False, 'Cleanup/restoration failed')
        require(abs(cleanup['deadline_monotonic']-cleanup['started_monotonic']-30) < 1e-6 and cleanup['deadline_remaining_seconds'] > 0, 'Cleanup fixed 30s budget exceeded')
        require([v['stage'] for v in cleanup['operations']] == ['visibility', 'snapshot', 'release_refs'], 'Wrong cleanup operations')
        require(all(v['status'] == 'returned' and v['remaining_before_seconds'] > 0 and v['remaining_after_seconds'] > 0 for v in cleanup['operations']), 'Cleanup operation failed/exhausted')
        require(data['cleanup_observer_refs_released'] is True and data['restored_all_shown'] is True, 'Final restoration unproved')
        formal = causal_paths | mask_paths | {initial_png, path.parent / 'final-all-shown.png'}
        require(set(path.parent.rglob('*.png')) == formal, 'PNG coverage omitted/additional files')
        self.counts.update({'cases': 1, 'features': len(fresh), 'causal_PNGs': len(causal_paths), 'mask_PNGs': len(mask_paths)})
        return {'case_id': data['case_id'], 'map_artifact_id': data['map_artifact_id'], 'feature_count': len(fresh),
                'observation_sha256': sha(path), 'passed': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path)
    parser.add_argument('--original', required=True, type=Path)
    parser.add_argument('--physical', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--case-limit', type=int, help='Explicit partial diagnostic only; never full1800 acceptance')
    parser.add_argument('--case-overrides', type=Path,
        help='Explicit case ID to fresh run root mapping; retains the original failed attempt unchanged')
    args = parser.parse_args()
    root, original = args.run.resolve(), args.original.resolve()
    require(root != original and not args.output.exists(), 'Output must be new and run must differ from original')
    physical_report = load(args.physical / 'physical-review.json')
    require(physical_report['passed'] is True and physical_report['counts']['maps'] == 1800, 'Physical1800 review incomplete')
    physical = {}
    with (args.physical / 'physical-features.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            entry = json.loads(line)
            require(entry['case_id'] not in physical, 'Duplicate physical case')
            physical[entry['case_id']] = entry
    require(len(physical) == len({r['map_artifact_id'] for r in physical.values()}) == 1800, 'Physical cohort not 1800 unique')
    spec = importlib.util.spec_from_file_location('full_run_observer_helpers', TOOL / 'audit_rendered_results.py')
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    paths = sorted(root.glob('shard-*/*/observation.json'))
    require(len({p.parent.name for p in paths}) == len(paths), 'Duplicate base case paths')
    overrides = load(args.case_overrides) if args.case_overrides else {}
    require(type(overrides) is dict and set(overrides) <= set(physical), 'Unknown override case IDs')
    base_paths = {p.parent.name: p for p in paths}
    require(set(overrides) <= set(base_paths), 'Override must replace an actual original attempt')
    origin_roots = {root}
    for case_id, replacement_root in overrides.items():
        require(type(replacement_root) is str and replacement_root, 'Invalid override root')
        replacement_root = Path(replacement_root).resolve()
        require(replacement_root != root and replacement_root != original, 'Retest needs a separate run root')
        replacement = replacement_root / physical[case_id]['shard'] / case_id / 'observation.json'
        require(replacement.is_file(), 'Missing actual retest observation')
        base_paths[case_id] = replacement
        origin_roots.add(replacement_root)
    paths = [base_paths[p.parent.name] for p in paths]
    reviewers = {origin: Reviewer(origin, original, h) for origin in origin_roots}
    if args.case_limit is not None:
        require(args.case_limit > 0, 'Positive diagnostic limit required')
        paths = paths[:args.case_limit]
    expected_ids, actual_ids = set(physical), {p.parent.name for p in paths}
    failures, cases = [], []
    started = time.perf_counter()
    for index, path in enumerate(paths, 1):
        try:
            origin = path.parent.parent.parent.resolve()
            reviewed = reviewers[origin].case(path, physical[path.parent.name])
            reviewed['origin_run'] = str(origin)
            cases.append(reviewed)
        except Exception as error:
            failure = {'case_id': path.parent.name, 'passed': False, 'error': type(error).__name__ + ': ' + str(error)[:1000]}
            failures.append(failure)
            cases.append(failure)
            print(json.dumps(failure, ensure_ascii=False), flush=True)
        if index % 25 == 0:
            print(json.dumps({'reviewed': index, 'failures': len(failures), 'elapsed_seconds': round(time.perf_counter()-started, 2)}), flush=True)
    complete = actual_ids == expected_ids and len(paths) == 1800 and args.case_limit is None
    counts, metrics = Counter(), {}
    for reviewer in reviewers.values():
        counts.update(reviewer.counts)
        for key, extrema in reviewer.metrics.items():
            target = metrics.setdefault(key, dict(extrema))
            target['min'] = min(target['min'], extrema['min'])
            target['max'] = max(target['max'], extrema['max'])
    result = {'passed': complete and not failures, 'partial_diagnostic': args.case_limit is not None,
        'all_selected_passed': bool(paths) and not failures, 'run': str(root), 'physical': str(args.physical.resolve()),
        'counts': dict(counts), 'missing_case_ids': sorted(expected_ids-actual_ids),
        'unexpected_case_ids': sorted(actual_ids-expected_ids), 'failures': failures,
        'maximum_coordinate_component_error_degrees': max(r.maximum_error for r in reviewers.values()), 'tolerance_degrees': 2e-6,
        'metrics': metrics, 'elapsed_seconds': time.perf_counter()-started,
        'case_overrides': overrides, 'retested_case_count': len(overrides),
        'original_attempts_rewritten': False,
        'cases': cases, 'large_summary_reads': 0, 'new_browser_or_provider_calls': 0,
        'scope': 'All selected saved actual SDK getters and four-phase PNG/mask/oracle/restore evidence replayed against this fresh independent GDAL1800 result; complete global native SDK/cancellation terminal validation remains separate.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({key: value for key, value in result.items() if key not in ('cases', 'missing_case_ids', 'metrics')}, ensure_ascii=False), flush=True)
    return 0 if result['passed'] or (args.case_limit is not None and result['all_selected_passed']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
