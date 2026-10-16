"""Replay complete closed Native SDK predicates for an explicitly selected cohort.

Each origin keeps its own complete Native ledger. A retest replaces a whole
isolated browser context, not an error flag or an individual network event.
"""
import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import time

TOOL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOL))
import audit_rendered_results as h

def require(value, message):
    if not value:
        raise ValueError(message)

def load(path):
    with Path(path).open(encoding='utf8') as stream:
        return json.load(stream)

def review(root, selected_ids, *, excluded_contexts=()):
    started = time.perf_counter()
    report = load(root / 'audit-summary.json')
    require(report['source_unchanged'] is True and report['evidence_completeness']['complete'] is True,
            'Origin source/export was not complete and unchanged')
    require(not report['worker_errors'] and not report['forbidden_requests'], 'Origin worker or egress failure')
    require(report['original_evidence_seal_before'] == report['original_evidence_seal_after']
            and report['original_evidence_seal_after']['passed'] is True, 'Original cohort seal failed')
    require(report['provider_requests_authorized'] == 0 and report['network_fulfillment'] is False,
            'Not an actual zero-Provider observation')
    require(report['sdk_final_verification_statistics']['witness_drift'] is None, 'Original Native authority drift')
    records = {r['case_id']: r for r in report['records']}
    require(len(records) == len(report['records']) and selected_ids <= set(records), 'Missing/duplicate selected case')
    if excluded_contexts:
        context_ids = {x.get('case_id') for x in report['network_request_start_observations']
                       if x.get('cdp_scope_id') in excluded_contexts}
        require(context_ids == set(records) - selected_ids, 'Retest did not replace exactly the whole isolated context')
    else:
        require(report['named_retest_passed'] is True and set(records) == selected_ids,
                'Fresh named retest was not complete and green')
        require(set(report['selection']['case_ids']) == selected_ids, 'Fresh retest selection differs')
    starts = report['network_request_start_observations']
    errors = deepcopy(report['background_task_errors'])
    start_report = load(root / 'audit-start.json')
    origins = [entry[key] for entry in start_report['server_map'].values()
               for key in ('frontend_url', 'backend_url')]
    h.reconcile_canceled_read_errors(errors, report['fetch_request_paused_observations'],
        report['network_loading_failed_observations'], origins, request_starts=starts)
    blocked = [e for e in h.gate_task_errors(errors) if e.get('cdp_scope_id') not in excluded_contexts]
    require(not blocked, 'A selected browser context has a blocking background error')
    witness = dict(zip(h.SDK_LEDGER_NAMES, (starts, report['native_sdk_response_observations'],
        report['native_sdk_actual_body_receipts'], report['network_request_extra_info_observations'],
        report['network_response_extra_info_observations'], report['network_loading_finished_observations'],
        report['native_sdk_initial_http200_receipts'])))
    verification = h.SdkVerificationPass(witness, closed=True)
    table = {}
    for item in report['sdk_receipt_authority']:
        digest = item['payload_sha256']
        require(digest not in table, 'Duplicate SDK authority entry')
        table[digest] = item
    payloads = {}
    def expand(ref):
        require(set(ref) == {'payload_sha256', 'position', 'owner', 'usage'}
                and type(ref['position']) is int and ref['position'] >= 0
                and type(ref['owner']) is dict and type(ref['usage']) is dict
                and set(ref['usage']) <= set(h.SDK_USAGE_FIELDS), 'Malformed SDK reference')
        digest = ref['payload_sha256']
        if digest not in payloads:
            item = table[digest]
            relative = 'sdk-receipts/' + digest + '.json'
            require(item['relative_path'] == relative, 'SDK authority path differs')
            payload = h.strict_read_json(h.checked_output_path(root, relative))
            encoded = h.strict_json_bytes(payload)
            require(hashlib.sha256(encoded).hexdigest() == digest
                    and encoded == h.strict_json_bytes(item['payload'])
                    and not set(payload).intersection(h.SDK_USAGE_FIELDS), 'SDK physical authority differs')
            payloads[digest] = payload
        payload = payloads[digest]
        require(h.strict_json_bytes(ref['owner']) == h.strict_json_bytes(
            {key: payload[key] for key in h.SDK_OWNER_FIELDS if key in payload}), 'SDK owner differs')
        return {**payload, **ref['usage']}
    global_digests, global_count = set(), 0
    for index, ref in enumerate(report['sdk_resources']):
        require(ref['position'] == index, 'Global SDK occurrence order differs')
        owner = ref['owner']
        if owner.get('cdp_scope_id') in excluded_contexts:
            continue
        resource = expand(ref)
        require(verification.verify(resource), 'Selected global SDK source predicate failed')
        global_digests.add(ref['payload_sha256']); global_count += 1
    cases, resource_occurrences, case_digests = [], 0, {}
    for index, case_id in enumerate(sorted(selected_ids), 1):
        stub = records[case_id]
        compact = h.load_thin_case(stub, root)
        h.require_terminal_case_consistency(stub, compact)
        require(compact['passed'] is True and compact['sdk_resource_native_identity_verified'] is True,
                'Selected terminal observation is red')
        resources = []
        for position, ref in enumerate(compact['sdk_resources']):
            # Global sdk_resources holds the final URL heads. An earlier case
            # can legitimately use the initial200 body before a later cached
            # representation replaces that head. The complete physical table
            # and closed Native ledger, not the final URL heads, are authority.
            require(ref['position'] == position and ref['payload_sha256'] in table,
                    'Case SDK reference was not covered by the complete physical authority')
            resource = expand(ref)
            require(verification.verify(resource), 'Selected case SDK predicate failed')
            resources.append(resource)
        hydrated = {**compact, 'sdk_resources': resources}
        require(resources and h.sdk_consumer_binding_verified(hydrated), 'SDK consumer/owner binding failed')
        require(compact['sdk_resource_consumer_scope']['cdp_scope_id'] not in excluded_contexts,
                'Excluded browser context leaked into the selected final cohort')
        require(not h.gate_task_errors(compact['background_task_errors']), 'Selected local error is blocking')
        cases.append(case_id); resource_occurrences += len(resources)
        case_digests[case_id] = stub['_case_record_ref']['file_sha256']
        if index % 100 == 0:
            print(json.dumps({'origin': root.name, 'native_cases_reviewed': index}, ensure_ascii=False), flush=True)
    require(verification.finish() and verification.drift is None, 'Closed Native witness changed during replay')
    return {'passed': True, 'root': str(root), 'selected_cases': len(cases), 'case_ids': cases,
        'observation_sha256': case_digests,
        'selected_global_SDK_responses': global_count, 'case_SDK_resource_occurrences': resource_occurrences,
        'unique_physical_SDK_payloads_checked': len(payloads), 'complete_predicate_calls': verification.predicate_calls,
        'full_native_ledger_counts': {key: len(value) for key, value in witness.items()},
        'excluded_entire_contexts': list(excluded_contexts), 'origin_full_map_gate': report['full_map_gate'],
        'original_failures_retained': [r['case_id'] for r in report['records'] if r['passed'] is not True],
        'original_gate_errors_retained': len(h.gate_task_errors(errors)),
        'source_before': report['source_before'], 'source_after': report['source_after'],
        'original_named_file_sha256': report['original_named_file_sha256'],
        'elapsed_seconds': time.perf_counter()-started}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--retest', type=Path, required=True)
    parser.add_argument('--case-ids', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), 'No overwrite of prior review')
    retested = set(load(args.case_ids))
    base_ids = {p.parent.name for p in args.base.glob('shard-*/*/observation.json')}
    require(len(base_ids) == 1800 and retested <= base_ids and len(retested) == 25,
            'Expected complete1800 plus the exact isolated25 retest')
    context_ids = set()
    for case_id in retested:
        path = next(args.base.glob('shard-*/' + case_id + '/observation.json'))
        context_ids.add(load(path)['sdk_resource_consumer_scope']['cdp_scope_id'])
    require(len(context_ids) == 1, 'Expected exactly one replaced isolated browser context')
    base = review(args.base, base_ids - retested, excluded_contexts=context_ids)
    retest = review(args.retest, retested)
    require(base['source_before'] == base['source_after'] == retest['source_before'] == retest['source_after'],
            'Application/browser source differs between original and retest')
    require(base['original_named_file_sha256'] == retest['original_named_file_sha256'], 'Original cohort differs')
    result = {'passed': True, 'accepted_case_count': base['selected_cases'] + retest['selected_cases'],
        'accepted_unique_case_count': len(set(base['case_ids']) | set(retest['case_ids'])),
        'attempt_case_count': 1825, 'original_attempt_rewritten': False,
        'no_new_browser_or_provider_calls': True, 'origins': [base, retest]}
    with args.output.open('x', encoding='utf8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({k:v for k,v in result.items() if k != 'origins'}, ensure_ascii=False), flush=True)

if __name__ == '__main__':
    main()
