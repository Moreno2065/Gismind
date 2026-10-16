"""Versioned ZERO-Provider observer of sealed historical results in real Chrome.

This file is independent audit instrumentation, outside sealed application and
browser-test source. It never starts servers, submits runs, uploads, resumes,
changes SDK constructors/styles/coordinates, or records new model successes.
Only prepared read-only clones and authentic AMap network resources are used.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter
from copy import deepcopy
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
import uuid
import weakref
import zipfile

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

TOOLING_ROOT = Path(__file__).resolve().parent
REPO_ROOT = next(parent for parent in TOOLING_ROOT.parents
    if (parent / 'backend/app').is_dir() and (parent / 'frontend/src').is_dir())
BACKEND_ROOT = REPO_ROOT / 'backend'
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(TOOLING_ROOT))
from sdk_geometry_oracle import verify_sdk_geometry
from visible_paint_oracle import verify_visible_paint

OBSERVER_VERSION = '2026-10-16.18'
MAX_CASES_PER_CONTEXT = 25
EVIDENCE_PUBLICATION_RETRIES = []
SDK_TIMEOUT_MS = 30000
PAINT_TIMEOUT_SECONDS = 30
EXPECTED_MAPS = 1800
EXPECTED_RASTERS = 200
EXPECTED_SEALED_FILES = 21515


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def publish_completed_json(temporary, path):
    """Windows scanners may briefly hold a completed file against replacement."""
    started = time.monotonic()
    deadline = started + 2.0
    retries = 0
    while True:
        try:
            os.replace(temporary, path)
            if retries:
                EVIDENCE_PUBLICATION_RETRIES.append({'path': str(path), 'retries': retries,
                    'elapsed_seconds': time.monotonic() - started})
            return
        except OSError as error:
            remaining = deadline - time.monotonic()
            if os.name != 'nt' or getattr(error, 'winerror', None) not in (5, 32, 33) or remaining <= 0:
                raise
            retries += 1
            # This consumes the caller's existing paint deadline; it never
            # creates a new paint budget or repeats a browser measurement.
            time.sleep(min(.05, remaining))


def write_json(path, value):
    """Stream the same JSON content, publishing only a complete owned temp file."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                prefix=path.name + '.', suffix='.pending', delete=False) as stream:
            temporary = Path(stream.name)
            for chunk in json.JSONEncoder(ensure_ascii=False, indent=2, allow_nan=False).iterencode(value):
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        publish_completed_json(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


SDK_LEDGER_NAMES = ('starts', 'responses', 'bodies', 'extra_infos', 'response_extra_infos', 'finished', 'prior_receipts')
SDK_USAGE_FIELDS = ('used_in_case_id', 'used_in_case_epoch', 'inherited_from_same_cdp_context')
SDK_OWNER_FIELDS = ('case_id', 'case_epoch', 'session_id', 'run_id', 'artifact_ids', 'map_artifact_id',
    'cdp_scope_id', 'page_target_id', 'request_id', 'frame_id', 'loader_id', 'raw_url_sha256')
CASE_BINDING_FIELDS = ('shard', 'case_id', 'run_id', 'session_id', 'map_artifact_id')
COMPACT_EVIDENCE_SCHEMA = 'historical-map-observer-lossless-refs-v1'


def strict_json_bytes(value):
    """Typed identity only: JSON-native finite values, no Python equality merging."""
    visiting = set()
    def check(item):
        kind = type(item)
        if kind in (type(None), bool, str, int):
            return
        if kind is float:
            if not math.isfinite(item):
                raise ValueError('non-finite strict JSON value')
            return
        if kind not in (dict, list):
            raise ValueError('unknown or subclass strict JSON value')
        if id(item) in visiting:
            raise ValueError('cyclic strict JSON value')
        visiting.add(id(item))
        if kind is dict:
            if any(type(key) is not str for key in item):
                raise ValueError('non-string strict JSON key')
            children = item.values()
        else:
            children = item
        for child in children:
            check(child)
        visiting.remove(id(item))
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def strict_read_json(path):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate JSON source key')
            result[key] = value
        return result
    value = json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON source')))
    strict_json_bytes(value)
    return value


def checked_output_path(root, relative):
    root = Path(root).resolve()
    if type(relative) is not str or not relative or Path(relative).is_absolute():
        raise ValueError('unbound evidence relative path')
    path = root / relative
    if not path.resolve().is_relative_to(root) or '..' in Path(relative).parts:
        raise ValueError('evidence escapes output root')
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('linked evidence path is not an owned ordinary file')
    return path


class SdkReceiptAuthority:
    """Lossless physical receipt payloads; references preserve usage/order/duplicates.

    Only three top-level non-predicate usage fields are factored out. SHA names
    are integrity identifiers, never substitutes for the original SDK proof.
    """
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.entries = {}

    def reference(self, receipt, position):
        if type(receipt) is not dict or type(position) is not int or position < 0:
            raise ValueError('unknown SDK receipt reference shape')
        strict_json_bytes(receipt)
        payload = {key: value for key, value in receipt.items() if key not in SDK_USAGE_FIELDS}
        encoded = strict_json_bytes(payload)
        digest = hashlib.sha256(encoded).hexdigest()
        relative = 'sdk-receipts/' + digest + '.json'
        path = checked_output_path(self.root, relative)
        if digest in self.entries and self.entries[digest] != encoded:
            raise ValueError('SDK payload digest collision with unequal full typed payload')
        if path.exists():
            if strict_json_bytes(strict_read_json(path)) != encoded:
                raise ValueError('SDK physical source collision or drift')
        else:
            path.parent.mkdir(exist_ok=True)
            write_json(path, payload)
        self.entries[digest] = encoded
        return {'payload_sha256': digest, 'position': position,
            'owner': {key: payload[key] for key in SDK_OWNER_FIELDS if key in payload},
            'usage': {key: receipt[key] for key in SDK_USAGE_FIELDS if key in receipt}}

    def expand(self, reference):
        strict_json_bytes(reference)
        if (type(reference) is not dict or set(reference) != {'payload_sha256', 'position', 'owner', 'usage'}
                or type(reference['position']) is not int or reference['position'] < 0
                or type(reference['owner']) is not dict or type(reference['usage']) is not dict
                or any(key not in SDK_USAGE_FIELDS for key in reference['usage'])
                or type(reference['payload_sha256']) is not str
                or not re.fullmatch(r'[0-9a-f]{64}', reference['payload_sha256'])):
            raise ValueError('invalid SDK receipt reference')
        digest = reference['payload_sha256']
        try:
            payload = strict_read_json(checked_output_path(self.root, 'sdk-receipts/' + digest + '.json'))
        except (OSError, ValueError) as error:
            raise ValueError('missing or malformed SDK authority source') from error
        encoded = strict_json_bytes(payload)
        if (type(payload) is not dict or any(key in payload for key in SDK_USAGE_FIELDS)
                or hashlib.sha256(encoded).hexdigest() != digest
                or (digest in self.entries and self.entries[digest] != encoded)
                or strict_json_bytes(reference['owner']) != strict_json_bytes(
                    {key: payload[key] for key in SDK_OWNER_FIELDS if key in payload})):
            raise ValueError('SDK source/owner integrity mismatch')
        self.entries[digest] = encoded
        return {**payload, **reference['usage']}

    def table(self):
        return [{'payload_sha256': digest, 'payload': json.loads(encoded),
            'relative_path': 'sdk-receipts/' + digest + '.json'} for digest, encoded in self.entries.items()]

    def compact_record(self, record):
        if '_sdk_receipt_schema' in record:
            raise ValueError('record already compact or unknown SDK schema')
        result = dict(record)
        if 'sdk_resources' in result:
            if type(result['sdk_resources']) is not list:
                raise ValueError('unknown SDK receipt collection')
            result['sdk_resources'] = [self.reference(value, index) for index, value in enumerate(result['sdk_resources'])]
            result['_sdk_receipt_schema'] = COMPACT_EVIDENCE_SCHEMA
        return result

    def expand_record(self, record):
        if '_sdk_receipt_schema' not in record:
            return dict(record)
        if record['_sdk_receipt_schema'] != COMPACT_EVIDENCE_SCHEMA or type(record.get('sdk_resources')) is not list:
            raise ValueError('unknown SDK compact record schema')
        result = {key: value for key, value in record.items() if key != '_sdk_receipt_schema'}
        if any(type(value) is not dict or value.get('position') != index
                or type(value.get('position')) is not int for index, value in enumerate(result['sdk_resources'])):
            raise ValueError('SDK receipt reference order mismatch')
        result['sdk_resources'] = [self.expand(value) for value in result['sdk_resources']]
        return result


def thin_case_record(record, root):
    if any(type(record.get(key)) is not str or not record[key] or '/' in record[key] or '\\' in record[key]
            or record[key] in ('.', '..') for key in CASE_BINDING_FIELDS):
        raise ValueError('case record identity is not bound')
    relative = record['shard'] + '/' + record['case_id'] + '/observation.json'
    path = checked_output_path(root, relative)
    thin = {key: value for key, value in record.items() if key != '_sdk_receipt_schema'
        and type(value) in (type(None), bool, str, int, float)}
    thin['_case_record_ref'] = {'schema': COMPACT_EVIDENCE_SCHEMA, 'relative_path': relative,
        'file_sha256': sha256_file(path), 'binding': {key: record[key] for key in CASE_BINDING_FIELDS}}
    return thin


def load_thin_case(record, root):
    reference = record.get('_case_record_ref')
    if (type(reference) is not dict or set(reference) != {'schema', 'relative_path', 'file_sha256', 'binding'}
            or reference.get('schema') != COMPACT_EVIDENCE_SCHEMA):
        raise ValueError('missing durable case record reference')
    binding = {key: record.get(key) for key in CASE_BINDING_FIELDS}
    if any(type(value) is not str or not value or '/' in value or '\\' in value or value in ('.', '..')
            for value in binding.values()):
        raise ValueError('durable case binding shape mismatch')
    if strict_json_bytes(binding) != strict_json_bytes(reference.get('binding')):
        raise ValueError('durable case reference identity mismatch')
    expected = binding['shard'] + '/' + binding['case_id'] + '/observation.json'
    if reference.get('relative_path') != expected:
        raise ValueError('durable case path mismatch')
    path = checked_output_path(root, expected)
    try:
        if sha256_file(path) != reference.get('file_sha256'):
            raise ValueError('durable case source integrity mismatch')
        actual = strict_read_json(path)
    except OSError as error:
        raise ValueError('durable case source missing') from error
    if type(actual) is not dict or any(actual.get(key) != binding[key] for key in CASE_BINDING_FIELDS):
        raise ValueError('durable case owner mismatch')
    return actual


def require_terminal_case_consistency(stub, record):
    """External reconstruction is terminal, not internal prior-file hydration."""
    if type(stub) is not dict or type(record) is not dict:
        raise ValueError('unknown terminal case shape')
    # Presence is authoritative: null/empty/false must not bypass incompleteness.
    if 'case_record_error' in stub:
        raise ValueError('incomplete terminal case; bound file is prior evidence, not a final record')
    actual = {key: value for key, value in stub.items() if key != '_case_record_ref'}
    expected = {key: value for key, value in record.items() if key != '_sdk_receipt_schema'
        and type(value) in (type(None), bool, str, int, float)}
    if strict_json_bytes(actual) != strict_json_bytes(expected):
        raise ValueError('terminal scalar presence/type/value differs from durable finalized record')


def expand_evidence_report(report, root):
    """Explicit opt-in reconstruction; never retain these large copies in live run."""
    if report.get('evidence_schema') != COMPACT_EVIDENCE_SCHEMA:
        raise ValueError('unknown evidence report reference schema')
    if 'evidence_completeness' in report:
        completeness = report['evidence_completeness']
        if (type(completeness) is not dict or set(completeness) != {'complete', 'errors'}
                or completeness.get('complete') is not True or type(completeness.get('errors')) is not list
                or completeness['errors']):
            raise ValueError('incomplete evidence report cannot be reconstructed as a complete terminal report')
    pool = SdkReceiptAuthority(root)
    seen = set()
    for source in report.get('sdk_receipt_authority', []):
        if type(source) is not dict or set(source) != {'payload_sha256', 'payload', 'relative_path'}:
            raise ValueError('unknown authority table entry')
        digest = source['payload_sha256']
        encoded = strict_json_bytes(source['payload'])
        if type(digest) is not str or digest in seen or hashlib.sha256(encoded).hexdigest() != digest:
            raise ValueError('duplicate or contradictory authority table source')
        if source['relative_path'] != 'sdk-receipts/' + digest + '.json':
            raise ValueError('authority table path mismatch')
        if strict_json_bytes(strict_read_json(checked_output_path(root, source['relative_path']))) != encoded:
            raise ValueError('authority table physical source drift')
        seen.add(digest); pool.entries[digest] = encoded
    result = {key: value for key, value in report.items() if key not in ('evidence_schema', 'sdk_receipt_authority')}
    references = report.get('sdk_resources', [])
    if type(references) is not list or any(value.get('position') != index
            or type(value.get('position')) is not int or value.get('payload_sha256') not in seen
            for index, value in enumerate(references)):
        raise ValueError('global SDK reference source/order mismatch')
    result['sdk_resources'] = [pool.expand(value) for value in references]
    result['records'] = []
    for stub in report.get('records', []):
        compact = load_thin_case(stub, root)
        if any(value.get('payload_sha256') not in seen for value in compact.get('sdk_resources', [])):
            raise ValueError('case SDK source omitted from global authority table')
        record = pool.expand_record(compact)
        require_terminal_case_consistency(stub, record)
        result['records'].append(record)
    return result


def validate_evidence_export(records, pool, global_references):
    """Bounded one-case-at-a-time resolution before atomic final publication."""
    for index, reference in enumerate(global_references):
        if reference.get('position') != index or reference.get('payload_sha256') not in pool.entries:
            raise ValueError('global export SDK source/order missing')
        pool.expand(reference)
    for stub in records:
        record = pool.expand_record(load_thin_case(stub, pool.root))
        require_terminal_case_consistency(stub, record)
    return True


def _position(value):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError('unsupported coordinate dimensions')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value):
        raise ValueError('non-finite coordinate')
    return value


def validate_map_scope(content):
    if content.get('crs') != {'kind': 'EPSG', 'code': 4326}:
        raise ValueError('map scope requires EPSG:4326')
    layers = content.get('layers')
    if not isinstance(layers, list) or len(layers) != 1:
        raise ValueError('map scope requires one FeatureCollection')
    layer = layers[0]
    if layer.get('type') != 'FeatureCollection' or 'style' in layer:
        raise ValueError('unsupported map layer or explicit style')
    features = layer.get('features')
    if not isinstance(features, list) or not features:
        raise ValueError('missing map features')
    for feature in features:
        if feature.get('type') != 'Feature' or '_source' in (feature.get('properties') or {}):
            raise ValueError('unsupported feature source/style')
        geometry = feature.get('geometry') or {}
        kind, coords = geometry.get('type'), geometry.get('coordinates')
        if kind == 'Point':
            _position(coords)
        elif kind == 'Polygon':
            if not isinstance(coords, list) or len(coords) != 1 or len(coords[0]) not in (65, 129):
                raise ValueError('scope requires one outer ring with 65/129 positions')
            for point in coords[0]:
                _position(point)
            if coords[0][0] != coords[0][-1]:
                raise ValueError('source polygon is not closed')
        else:
            raise ValueError(f'unsupported geometry type {kind!r}')
    return features


def bind_case(row, result, case_root):
    case_root = Path(case_root)
    snapshot = result.get('snapshot') or {}
    if (row.get('case_id') != result.get('case_id') or row.get('run_id') != result.get('run_id')
            or result.get('run_id') != snapshot.get('run_id')
            or not result.get('session_id') or result['session_id'] != snapshot.get('session_id')
            or case_root.name != result.get('case_id') or Path(row.get('evidence_path', '')).resolve() != case_root.resolve()):
        raise ValueError('case/run/session/evidence binding mismatch')
    if not all(value.get('passed') is True and value.get('real_provider_verified') is True for value in (row, result)):
        raise ValueError('original case was not an authenticated historical success')
    if snapshot.get('status') != 'completed':
        raise ValueError('original run is not completed')
    artifacts = snapshot.get('output_artifacts', [])
    ids = [value.get('artifact_id') for value in artifacts]
    final_ids = snapshot.get('final_artifact_ids', [])
    if len(ids) != len(set(ids)) or len(final_ids) != len(set(final_ids)) or not set(final_ids) <= set(ids):
        raise ValueError('ambiguous final artifact binding')
    final = [value for value in artifacts if value['artifact_id'] in final_ids]
    maps = [value for value in final if value.get('type') == 'MapLayer']
    if not maps:
        if len(final) == 1 and final[0].get('type') == 'Raster':
            return {'excluded': 'Raster', 'case_id': row['case_id'], 'raster_artifact_id': final[0]['artifact_id']}
        raise ValueError('one final MapLayer or explicit final Raster is required')
    files = [value for value in final if value.get('type') == 'File']
    if len(maps) != 1 or len(files) != 1 or len(final) != 2:
        raise ValueError('one final MapLayer and one final GPKG are required')
    file_schema = (files[0].get('schema') or {}).get('file') or {}
    if file_schema.get('extension') != '.gpkg' or file_schema.get('format') != 'gpkg':
        raise ValueError('final download is not a declared GPKG')
    map_path = case_root / (maps[0]['artifact_id'] + '.json')
    gpkg_path = case_root / (files[0]['artifact_id'] + '.gpkg')
    for artifact, path in ((maps[0], map_path), (files[0], gpkg_path)):
        if not path.is_file() or path.is_symlink() or sha256_file(path) != artifact.get('content_hash'):
            raise ValueError('local downloaded artifact/content hash mismatch')
    with gpkg_path.open('rb') as stream:
        if stream.read(16) != b'SQLite format 3\x00':
            raise ValueError('local final download is not physical SQLite/GPKG bytes')
    content = read_json(map_path)
    if maps[0].get('crs') != content.get('crs'):
        raise ValueError('MapLayer metadata/content CRS binding mismatch')
    validate_map_scope(content)
    return {'case_id': row['case_id'], 'run_id': row['run_id'], 'session_id': result['session_id'],
            'shard': case_root.parent.name, 'case_root': case_root, 'map_artifact': maps[0],
            'file_artifact': files[0], 'map_path': map_path, 'gpkg_path': gpkg_path,
            'original_output_artifacts': deepcopy(artifacts),
            'result_sha256': sha256_file(case_root / 'result.json') if (case_root / 'result.json').exists() else None}


def trace_user_id(path):
    users = set()
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.endswith('.network'):
                continue
            for line in archive.read(name).splitlines():
                request = json.loads(line).get('snapshot', {}).get('request', {})
                if not urlsplit(request.get('url', '')).path.startswith('/api/'):
                    continue
                for header in request.get('headers', []):
                    if header.get('name', '').lower() == 'x-user-id' and header.get('value'):
                        users.add(header['value'])
    if len(users) != 1:
        raise ValueError('trace must contain exactly one actual X-User-Id')
    return users.pop()


def normalize_sdk_overlay(overlay):
    value = deepcopy(overlay)
    kind, raw = value.get('kind'), value.get('raw_coordinates')
    if kind == 'Point':
        value['coordinates'] = deepcopy(_position(raw))
    elif kind == 'Polygon':
        if not isinstance(raw, list) or not raw:
            raise ValueError('SDK polygon path is empty')
        rings = [raw] if isinstance(raw[0], list) and raw[0] and isinstance(raw[0][0], (int, float)) else raw
        if len(rings) != 1:
            raise ValueError('SDK polygon scope excludes holes')
        closed = []
        for ring in rings:
            if not isinstance(ring, list) or len(ring) < 3:
                raise ValueError('SDK polygon path is unobservable')
            points = [deepcopy(_position(point)) for point in ring]
            if points[-1] != points[0]:
                points.append(deepcopy(points[0]))
            closed.append(points)
        value['coordinates'] = closed
    else:
        raise ValueError('unsupported SDK overlay type')
    return value


def build_masks(kind, projected_coordinates, shape, *, dpr=1.0):
    height, width = shape
    if dpr != 1.0:
        raise ValueError('observer requires explicit CSS-scale screenshots (DPR recorded separately)')
    recipe = {'basis': 'independent-GPKG -> EPSG4326 -> explicit GCJ02 -> real map.lngLatToContainer',
              'screenshot_scale': 'css', 'core_erode_pixels': 3, 'allowed_dilation_pixels': 5}
    fill = Image.new('L', (width, height), 0)
    contour = None
    draw = ImageDraw.Draw(fill)
    if kind == 'Point':
        x, y = _position(projected_coordinates)
        x -= 8; y -= 8
        if x - 20 <= 0 or y - 20 <= 0 or x + 20 >= width - 1 or y + 20 >= height - 1:
            raise ValueError('marker allowed extent touches crop edge')
        draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=255)
        allowed = Image.new('L', (width, height), 0)
        ImageDraw.Draw(allowed).ellipse((x - 20, y - 20, x + 20, y + 20), fill=255)
        core = np.array(fill) != 0
        recipe.update({'marker_offset': [-8, -8], 'anchor': 'center', 'core_radius_pixels': 5, 'allowed_radius_pixels': 20})
    elif kind == 'Polygon':
        if len(projected_coordinates) != 1:
            raise ValueError('mask scope excludes holes')
        points = [tuple(_position(point)) for point in projected_coordinates[0]]
        if any(x - 5 <= 0 or y - 5 <= 0 or x + 5 >= width - 1 or y + 5 >= height - 1 for x, y in points):
            raise ValueError('polygon allowed extent touches crop edge')
        draw.polygon(points, fill=255)
        core = np.array(fill.filter(ImageFilter.MinFilter(7))) != 0
        perimeter = Image.new('L', (width, height), 0)
        ImageDraw.Draw(perimeter).line(points, fill=255, width=1)
        contour = np.array(perimeter) != 0
        allowed = fill.filter(ImageFilter.MaxFilter(11))
        recipe['contour_width_pixels'] = 1
    else:
        raise ValueError('unsupported mask geometry')
    if np.count_nonzero(core) < 25:
        raise ValueError('insufficient independent core pixels')
    return {'core_mask': core, 'contour_mask': contour, 'allowed_mask': np.array(allowed) != 0, 'recipe': recipe}


def coverage_gate(expected_ids, records, *, raster_excluded, source_unchanged, limit, background_task_errors=(), evidence_seal_verified=False):
    actual_ids = [value.get('map_artifact_id') for value in records]
    expected_set = set(expected_ids)
    actual_set = set(actual_ids)
    complete = (len(expected_ids) == EXPECTED_MAPS == len(expected_set) == len(actual_ids) == len(actual_set)
                and actual_set == expected_set and all(value.get('passed') is True for value in records)
                and raster_excluded == EXPECTED_RASTERS and source_unchanged is True and limit is None
                and not gate_task_errors(background_task_errors) and evidence_seal_verified is True)
    return {'full_map_gate': complete, 'expected_map_count': len(expected_ids), 'audited_map_count': len(actual_ids),
            'unique_audited_map_count': len(actual_set), 'passed_map_count': sum(value.get('passed') is True for value in records),
            'missing_map_ids': sorted(expected_set - actual_set), 'unexpected_map_ids': sorted(actual_set - expected_set),
            'raster_excluded_count': raster_excluded, 'pilot_limit': limit,
            'evidence_seal_verified': evidence_seal_verified,
            'background_task_error_count': len(background_task_errors),
            'blocking_background_task_error_count': len(gate_task_errors(background_task_errors)),
            'confirmed_canceled_read_count': sum(_verified_canceled_read_proof(value) for value in background_task_errors)}


def redact_url(url):
    value = urlsplit(url)
    hostname = value.hostname or ''
    port = f':{value.port}' if value.port else ''
    query = [(key, val if key.lower() in {'v', 'version'} else '[REDACTED]') for key, val in parse_qsl(value.query, keep_blank_values=True)]
    return urlunsplit((value.scheme, hostname + port, value.path, urlencode(query), ''))


def blocked_request(method, url, origins):
    value = urlsplit(url)
    if (value.hostname or '').lower() == 'api.deepseek.com':
        return True
    origin = urlunsplit((value.scheme, value.netloc, '', '', ''))
    return origin in {item.rstrip('/') for item in origins} and value.path.startswith('/api/') and method.upper() not in {'GET', 'HEAD', 'OPTIONS'}


def validate_output(original, output):
    original, output = Path(original).resolve(), Path(output).resolve()
    if output.exists() or output.parent != original.parent or output == original:
        raise ValueError('output must be a new unique sibling outside original evidence')
    for node in (output, *output.parents):
        if node.exists() and (node.is_symlink() or (hasattr(node, 'is_junction') and node.is_junction())):
            raise ValueError('output must not traverse links/junctions')
    return output


def _color(value):
    normalized = str(value).lower().replace(' ', '')
    return normalized in {'#8a96a6', 'rgb(138,150,166)', 'rgba(138,150,166,1)'}


def validate_overlay_style(overlay):
    options = overlay.get('options') or {}
    if overlay['kind'] == 'Point':
        content = re.sub(r'\s+', '', overlay.get('content') or '').lower()
        required = ('width:16px', 'height:16px', 'border-radius:50%', 'background:#8a96a6',
                    'box-shadow:0003px#5b677633,008px#8a96a6aa')
        if options.get('anchor') != 'center' or options.get('offset') != [-8, -8] or not all(value in content for value in required):
            raise ValueError('unsupported actual marker style/offset')
    elif overlay['kind'] == 'Polygon':
        if (not _color(options.get('fillColor')) or not _color(options.get('strokeColor'))
                or not math.isclose(options.get('fillOpacity', -1), .24, abs_tol=1e-9)
                or options.get('strokeWeight') != 1.5 or options.get('strokeOpacity') != .9):
            raise ValueError('unsupported actual polygon style')
    else:
        raise ValueError('unsupported actual overlay style/type')


def independent_expected_features(gpkg_path):
    import geopandas as gpd
    from shapely.geometry import mapping
    from app.tools.geo_transform import wgs84_to_gcj02
    frame = gpd.read_file(gpkg_path)
    if frame.empty or frame.crs is None or not frame.geometry.is_valid.all() or frame.geometry.is_empty.any():
        raise ValueError('independent physical GPKG is empty, invalid, or lacks CRS')
    geographic = frame.to_crs(4326)
    features = []
    for geometry in geographic.geometry:
        value = json.loads(json.dumps(mapping(geometry)))
        if value['type'] == 'Point':
            coords = list(wgs84_to_gcj02(*_position(value['coordinates'])))
        elif value['type'] == 'Polygon' and len(value['coordinates']) == 1 and len(value['coordinates'][0]) in (65, 129):
            coords = [[list(wgs84_to_gcj02(*_position(point))) for point in value['coordinates'][0]]]
        else:
            raise ValueError('independent physical GPKG geometry exceeds audited scope')
        features.append({'type': 'Feature', 'properties': {}, 'geometry': {'type': value['type'], 'coordinates': coords}})
    return features


def verify_clone_snapshot(bound, snapshot):
    final_ids = [bound['map_artifact']['artifact_id'], bound['file_artifact']['artifact_id']]
    if (snapshot.get('run_id') != bound['run_id'] or snapshot.get('session_id') != bound['session_id']
            or snapshot.get('status') != 'completed' or sorted(snapshot.get('final_artifact_ids', [])) != sorted(final_ids)):
        raise ValueError('clone run/session/final artifact binding mismatch')
    artifacts = snapshot.get('output_artifacts') or []
    if len({item['artifact_id'] for item in artifacts}) != len(artifacts):
        raise ValueError('clone duplicate output artifact identity')
    indexed = {item['artifact_id']: item for item in artifacts}
    original_artifacts = bound['original_output_artifacts']
    if set(indexed) != {item['artifact_id'] for item in original_artifacts}:
        raise ValueError('clone output_artifacts differ from original IDs')
    for original in original_artifacts:
        current = indexed.get(original['artifact_id'])
        if current is None or any(current.get(key) != original.get(key) for key in ('artifact_id', 'type', 'content_hash', 'crs', 'schema')):
            raise ValueError('clone metadata/content hash binding mismatch')


def validate_original_summary(summary):
    if (any(summary.get(name) != 2000 for name in ('scheduled', 'executed', 'passed_cases'))
            or summary.get('passed') is not True or summary.get('full_2000_gate') is not True
            or summary.get('network_stubs') is not False or summary.get('source_unchanged') is not True):
        raise ValueError('original must have explicit terminal 2000-pass gate with unchanged source and no stubs')


def source_hashes():
    files = []
    for directory in ('backend/app', 'backend/tests/browser', 'backend/tests/live', 'frontend/src', 'frontend/scripts', '.github/workflows'):
        files.extend(path for path in (REPO_ROOT / directory).rglob('*') if path.is_file() and '__pycache__' not in path.parts)
    for pattern in ('backend/requirements*', 'backend/constraints*', 'frontend/package*.json', 'frontend/*config*', 'backend/.env*', 'frontend/.env*'):
        files.extend(path for path in REPO_ROOT.glob(pattern) if path.is_file())
    return {path.relative_to(REPO_ROOT).as_posix(): sha256_file(path) for path in sorted(set(files))}


def verify_evidence_seal(root):
    """Run the original read-only all-file verifier, without resealing anything."""
    from tests.browser.runtime_live_browser import verify_evidence_seal as verify_original_evidence_seal
    root = Path(root).resolve()
    verify_original_evidence_seal(root)
    manifest_path = root / 'evidence-manifest.json'
    manifest = read_json(manifest_path)
    return {'passed': True, 'verified_file_count': len(manifest),
            'manifest_sha256': sha256_file(manifest_path),
            'basis': 'original verify_evidence_seal: exact all-file relative paths and byte SHA256 equality'}


def view_signature(snapshot):
    view = snapshot.get('view')
    if not isinstance(view, dict) or not all(name in view for name in ('center', 'zoom', 'pitch', 'rotation', 'size', 'rect', 'dpr')):
        raise ValueError('camera/layout snapshot is incomplete')
    try:
        _position(view['center'])
        _position(view['size'])
        numbers = [view[name] for name in ('zoom', 'pitch', 'rotation', 'dpr')]
        rect = view['rect']
        numbers.extend(rect[name] for name in ('x', 'y', 'width', 'height'))
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in numbers):
            raise ValueError('non-finite camera value')
        if min(*view['size'], rect['width'], rect['height'], view['dpr']) <= 0:
            raise ValueError('non-positive camera/layout dimensions')
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError('camera/layout size, projection or crop is unobservable') from error
    return json.dumps(view, sort_keys=True, allow_nan=False, separators=(',', ':'))


def require_fixed_view(signature, snapshot):
    if view_signature(snapshot) != signature:
        raise ValueError('camera/layout moved during observation')


# Executed on the actual Runtime.queryObjects result, without constructors or
# prototype wrapping. Only references to existing SDK instances are retained.
SDK_ATTACH_JS = r'''function(artifactId) {
  const candidates = this.filter(map => {
    const node = map.getContainer?.();
    if (!node?.isConnected) return false;
    try { return JSON.parse(node.getAttribute('data-map-render-evidence') || '{}').sourceArtifactId === artifactId; }
    catch { return false; }
  });
  if (candidates.length !== 1) throw Error('exactly one connected source-bound actual SDK map is required');
  const map = candidates[0];
  const markers = map.getAllOverlays('marker').filter(o => o.getMap() === map);
  const polygons = map.getAllOverlays('polygon').filter(o => o.getMap() === map);
  const all = map.getAllOverlays().filter(o => o.getMap() === map);
  if (all.length !== markers.length + polygons.length) throw Error('unsupported attached actual overlay kind');
  window.__gismindHistoricalObserver = {map, overlays: [...markers, ...polygons],
    kinds: [...markers.map(() => 'Point'), ...polygons.map(() => 'Polygon')], artifactId};
  return {candidateCount: candidates.length, overlayCount: all.length};
}'''

SDK_PIXEL_FUNCTION_JS = r'''value => {
  if (Array.isArray(value)) {
    if (value.length !== 2 || !value.every(v => typeof v === 'number' && Number.isFinite(v)))
      throw Error('unreadable actual SDK pixel array');
    return [value[0],value[1]];
  }
  const x = value?.x ?? (typeof value?.getX === 'function' ? value.getX() : undefined);
  const y = value?.y ?? (typeof value?.getY === 'function' ? value.getY() : undefined);
  if (typeof x !== 'number' || typeof y !== 'number' || !Number.isFinite(x) || !Number.isFinite(y))
    throw Error('unreadable actual SDK pixel');
  return [x,y];
}'''

SDK_MEMBERSHIP_FUNCTION_JS = r'''(expected,kinds,markers,polygons,all) => {
  const sameSet = (left,right) => {
    if (!Array.isArray(left) || !Array.isArray(right)) return false;
    if (new Set(left).size !== left.length || new Set(right).size !== right.length || left.length !== right.length) return false;
    return left.every(value => right.includes(value));
  };
  if (expected.length !== kinds.length || kinds.some(kind => kind !== 'Point' && kind !== 'Polygon'))
    throw Error('invalid retained actual SDK type scope');
  const expectedMarkers=expected.filter((_,i)=>kinds[i]==='Point');
  const expectedPolygons=expected.filter((_,i)=>kinds[i]==='Polygon');
  if (!sameSet(expectedMarkers,markers) || !sameSet(expectedPolygons,polygons) || !sameSet(expected,all)
      || !sameSet([...markers,...polygons],all))
    throw Error('current actual SDK overlay membership/type set changed');
  return {markerCount:markers.length,polygonCount:polygons.length,allCount:all.length,matched:true};
}'''

SDK_SNAPSHOT_JS = r'''() => {
  const state = window.__gismindHistoricalObserver;
  if (!state) throw Error('actual SDK observation refs unavailable');
  const map = state.map, node = map.getContainer();
  if (!node.isConnected) throw Error('actual SDK map detached');
  const evidence = JSON.parse(node.getAttribute('data-map-render-evidence') || '{}');
  if (evidence.sourceArtifactId !== state.artifactId) throw Error('actual SDK source identity changed');
  const membership = (__MEMBERSHIP__)(state.overlays,state.kinds,
    map.getAllOverlays('marker'),map.getAllOverlays('polygon'),map.getAllOverlays());
  const pair = value => {
    if (Array.isArray(value)) return value.map(pair);
    if (value && typeof value.getLng === 'function') return [value.getLng(), value.getLat()];
    if (typeof value === 'number') return value;
    throw Error('unreadable actual SDK coordinates');
  };
  const pixel = (__PIXEL__);
  const rect = node.getBoundingClientRect();
  const size = map.getSize();
  const overlays = state.overlays.map((overlay, i) => {
    if (overlay.getMap() !== map) throw Error('actual overlay detached');
    const options = overlay.getOptions();
    const safeOptions = {};
    for (const key of ['anchor','fillColor','fillOpacity','strokeColor','strokeWeight','strokeOpacity']) {
      if (options[key] !== undefined) safeOptions[key] = options[key];
    }
    if (options.offset !== undefined) safeOptions.offset = pixel(options.offset);
    const content = state.kinds[i] === 'Point' ? overlay.getContent() : null;
    let visible = typeof overlay.getVisible === 'function' ? overlay.getVisible() : options.visible;
    visible = typeof visible === 'boolean' ? visible : null;
    return {overlay_index:i, kind:state.kinds[i],
      raw_coordinates:pair(state.kinds[i] === 'Point' ? overlay.getPosition() : overlay.getPath()),
      options:safeOptions, content:typeof content === 'string' ? content : content?.outerHTML,
      visibility_observable:visible !== null, visible};
  });
  return {evidence, membership, overlays, view:{center:pair(map.getCenter()), zoom:map.getZoom(),
    pitch:map.getPitch(), rotation:map.getRotation(), size:[size.width,size.height],
    rect:{x:rect.x,y:rect.y,width:rect.width,height:rect.height},dpr:devicePixelRatio}};
}'''.replace('__MEMBERSHIP__', SDK_MEMBERSHIP_FUNCTION_JS).replace('__PIXEL__', SDK_PIXEL_FUNCTION_JS)

SDK_PROJECT_JS = r'''features => {
  const map = window.__gismindHistoricalObserver.map;
  const project = value => {
    if (Array.isArray(value) && value.length === 2 && value.every(Number.isFinite)) {
      const p = map.lngLatToContainer(value);
      return (__PIXEL__)(p);
    }
    return value.map(project);
  };
  return features.map(feature => ({kind:feature.geometry.type,coordinates:project(feature.geometry.coordinates)}));
}'''.replace('__PIXEL__', SDK_PIXEL_FUNCTION_JS)

SDK_VISIBILITY_JS = r'''({index, shown}) => {
  const state = window.__gismindHistoricalObserver;
  state.overlays.forEach((overlay, i) => {
    if (shown && (index === null || i === index)) overlay.show(); else overlay.hide();
  });
}'''


def parse_cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--server-map', required=True, type=Path)
    parser.add_argument('--concurrency', type=int, default=4, choices=range(1, 5))
    parser.add_argument('--limit', type=int)
    parser.add_argument('--case-ids-file', type=Path,
        help='Retest exactly these original case IDs in original order, in a new output directory')
    parser.add_argument('--max-failures', type=int,
        help='cooperatively stop scheduling new cases after this many failed cases; in-flight cases finish')
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive; every limited run is a pilot')
    if args.limit is not None and args.case_ids_file is not None:
        parser.error('--limit and --case-ids-file cannot be combined')
    if args.max_failures is not None and args.max_failures < 1:
        parser.error('--max-failures must be positive')
    return args


def select_original_cases(cases, *, limit=None, case_ids_file=None):
    if case_ids_file is None:
        return cases if limit is None else cases[:limit]
    if limit is not None:
        raise ValueError('named retest cannot also use a limit')
    requested = strict_read_json(case_ids_file)
    if (type(requested) is not list or not requested
            or any(type(value) is not str or not value for value in requested)
            or len(set(requested)) != len(requested)):
        raise ValueError('retest case IDs must be a nonempty unique string list')
    available = {case['case_id'] for case in cases}
    if not set(requested) <= available:
        raise ValueError('retest contains an unknown original case ID')
    return [case for case in cases if case['case_id'] in set(requested)]


class CooperativeFailureBudget:
    """Single-event-loop scheduling boundary; never cancels in-flight work."""
    def __init__(self, maximum):
        if maximum is not None and (type(maximum) is not int or maximum < 1):
            raise ValueError('max failures must be a positive integer or None')
        self.maximum = maximum
        self.started = []
        self.completed = []
        self.failed = []
        self.triggered = False

    def start(self, case_id):
        if self.triggered:
            return False
        if case_id in self.started:
            raise ValueError('duplicate cooperative case scheduling')
        self.started.append(case_id)
        return True

    def finish(self, case_id, passed):
        if case_id not in self.started or case_id in self.completed:
            raise ValueError('unbound or duplicate cooperative case completion')
        self.completed.append(case_id)
        if passed is not True:
            self.failed.append(case_id)
            if self.maximum is not None and len(self.failed) >= self.maximum:
                self.triggered = True

    def report(self, cases):
        unscheduled = [case['case_id'] for case in cases if case['case_id'] not in self.started]
        return {'enabled': self.maximum is not None, 'max_failures': self.maximum,
            'triggered': self.triggered, 'failure_count': len(self.failed), 'failed_case_ids': list(self.failed),
            'started_case_count': len(self.started), 'started_case_ids': list(self.started),
            'completed_case_count': len(self.completed), 'completed_case_ids': list(self.completed),
            'unscheduled_case_count': len(unscheduled), 'unscheduled_case_ids': unscheduled,
            'inflight_policy': 'finish current cases, cleanup and export complete retained global ledgers; never cancel'}


async def observe_scheduled_cases(cases, audit_case, budget):
    for case in cases:
        if not budget.start(case['case_id']):
            break
        try:
            record = await audit_case(case)
        except Exception:
            budget.finish(case['case_id'], False)
            raise
        budget.finish(case['case_id'], record.get('passed'))
        yield record


class PaintCaptureError(ValueError):
    def __init__(self, message, *, failure_kind='paint_budget_exhausted'):
        super().__init__(message)
        self.failure_kind = failure_kind


def require_paint_deadline(deadline, stage):
    if time.monotonic() >= deadline:
        error = PaintCaptureError('fixed paint budget exhausted at ' + stage)
        error.stage = stage
        raise error


def observation_passed(*, geometry, pixels, restored, sdk_identity, forbidden_requests, background_task_errors=(), session_projection_verified=False):
    return (all(value is True for value in (geometry, pixels, restored, sdk_identity, session_projection_verified))
            and not forbidden_requests and not gate_task_errors(background_task_errors))


HISTORY_SESSION_HEADER = 'X-History-Observer-Session'
HISTORY_POLICY_HEADER = 'X-History-Observer-Policy'
HISTORY_POLICY_VERSION = 'history-readonly-observer-v2'


def _projection_json(body):
    if not isinstance(body, bytes):
        raise ValueError('projection requires actual response bytes')
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError('projection response is not valid JSON bytes') from error


def _session_items(value):
    if not isinstance(value, dict) or set(value) != {'items'} or not isinstance(value['items'], list):
        raise ValueError('projection session collection has unsupported shape')
    items = value['items']
    for item in items:
        if not isinstance(item, dict) or set(item) != {'id', 'title', 'created_at', 'updated_at'}:
            raise ValueError('projection metadata has unsupported fields')
        if any(not isinstance(item[name], str) or not item[name] for name in item):
            raise ValueError('projection metadata contains an unobservable public field')
    if len({item['id'] for item in items}) != len(items):
        raise ValueError('projection contains duplicate session identities')
    return items


def _session_membership_sha256(items):
    data = json.dumps({item['id']: item for item in items}, sort_keys=True,
        ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def make_projection_reference(*, selector, frontend_origin, backend_origin, stock_url, stock_status,
                              stock_body, selected_url, selected_status, selected_body):
    for origin in (frontend_origin, backend_origin):
        parsed = urlsplit(origin)
        if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'} or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError('projection reference origin is not a credential-free local endpoint')
    if (stock_status != 200 or stock_url != backend_origin + '/api/sessions'
            or selected_status != 200 or selected_url != backend_origin + '/api/sessions/' + selector):
        raise ValueError('projection reference stock/selected endpoint or status mismatch')
    recent = _session_items(_projection_json(stock_body))
    if len(recent) != 200:
        raise ValueError('projection reference must independently preserve stock recent 200 rows')
    selected = _projection_json(selected_body)
    _session_items({'items': [selected]})
    if selected['id'] != selector:
        raise ValueError('projection selected reference metadata differs from original case session')
    indexed = {item['id']: item for item in recent}
    if selector in indexed and indexed[selector] != selected:
        raise ValueError('projection selected reference contradicts stock recent metadata')
    return {'selector_session_id': selector, 'frontend_origin': frontend_origin, 'backend_origin': backend_origin,
        'stock_url': stock_url, 'stock_status': stock_status, 'selected_url': selected_url, 'selected_status': selected_status,
        'selected_actual_item': selected, 'selected_metadata_body_sha256': hashlib.sha256(selected_body).hexdigest(),
        'original_recent_items': recent, 'original_recent_ids': [item['id'] for item in recent],
        'original_recent_count': len(recent), 'stock_recent_body_sha256': hashlib.sha256(stock_body).hexdigest(),
        'original_recent_membership_sha256': _session_membership_sha256(recent)}


def verify_session_projection(reference, *, url, status, headers, body):
    normalized_headers = {name.lower(): value for name, value in headers.items()}
    selector = reference['selector_session_id']
    if (url != reference['frontend_origin'] + '/api/sessions' or status != 200
            or normalized_headers.get(HISTORY_POLICY_HEADER.lower()) != HISTORY_POLICY_VERSION
            or normalized_headers.get(HISTORY_SESSION_HEADER.lower()) != selector):
        raise ValueError('projection browser response endpoint/status/policy/echo binding mismatch')
    projected = _session_items(_projection_json(body))
    expected = {item['id']: item for item in reference['original_recent_items']}
    expected[selector] = reference['selected_actual_item']
    observed = {item['id']: item for item in projected}
    if observed != expected:
        raise ValueError('projection browser response changed selected/recent metadata or added/missing rows')
    return {**deepcopy(reference), 'passed': True, 'response_url': redact_url(url), 'response_status': status,
        'response_policy': normalized_headers[HISTORY_POLICY_HEADER.lower()], 'response_selector_echo': selector,
        'response_body_sha256': hashlib.sha256(body).hexdigest(), 'projected_count': len(projected),
        'projected_ids': [item['id'] for item in projected], 'projected_membership_sha256': _session_membership_sha256(projected),
        'original_recent_all_rows_preserved': True, 'selected_item_matches_exact_metadata_endpoint': True,
        'deduplicated_session_identities': True, 'stock_application_changed': False}


def projection_observations_verified(observations, selector):
    return bool(observations) and all(value.get('passed') is True
        and value.get('selector_session_id') == selector
        and value.get('response_selector_echo') == selector
        and value.get('response_policy') == HISTORY_POLICY_VERSION
        and value.get('original_recent_all_rows_preserved') is True
        and value.get('deduplicated_session_identities') is True
        for value in observations)


def history_projection_headers(request, origins, session_id):
    parsed = urlsplit(request['url'])
    origin = urlunsplit((parsed.scheme, parsed.netloc, '', '', ''))
    if request['method'].upper() != 'GET' or parsed.path != '/api/sessions' or origin not in origins:
        return None
    if not isinstance(session_id, str) or not session_id:
        raise ValueError('historical session projection requires the current original session binding')
    headers = [{'name': name, 'value': str(value)} for name, value in request.get('headers', {}).items()
               if name.lower() != HISTORY_SESSION_HEADER.lower()]
    headers.append({'name': HISTORY_SESSION_HEADER, 'value': session_id})
    return headers


def track_task(awaitable, pending, errors, metadata, *, on_error=None):
    """Retrieve every actual asynchronous task exception before discarding refs."""
    task = asyncio.create_task(awaitable)
    pending.add(task)
    captured = deepcopy(metadata)

    def persist(error, classification):
        try:
            message = safe_error(error)
        except Exception:
            message = type(error).__name__ + ': exception formatting unavailable'
        errors.append({**captured, 'error': message, 'classification': classification,
                       'request_cancellation_confirmed': False, 'gate_failure': True})

    def completed(done):
        pending.discard(done)
        try:
            if done.cancelled():
                error = asyncio.CancelledError('observer background task was cancelled')
                classification = 'observer_task_cancelled'
            else:
                error = done.exception()
                if error is None:
                    return
                try:
                    classification = ('invalid_interception_id' if 'Invalid InterceptionId' in str(error)
                                      else 'observer_background_task_exception')
                except Exception:
                    # Formatting cannot replace the actual task exception.
                    classification = 'observer_background_task_exception'
            persist(error, classification)
        except Exception as error:
            persist(error, 'observer_task_completion_exception')
        # Native evidence can precede this task's completion. Persist the original
        # exception first, then replay the complete authority without event gating.
        if on_error is not None:
            try:
                on_error()
            except Exception as error:
                persist(error, 'observer_error_callback_exception')

    task.add_done_callback(completed)
    return task


def event_relation_requires_full(event_kind, record, errors):
    """O(errors), no cache: only fully known, disjoint original OR IDs may skip.

    Matching IDs conservatively replay even in a foreign scope. Case/epoch,
    URL, resource type, classification and confirmation are never exclusions.
    """
    relations = {'start': (('request_id', 'network_id'),),
        'paused': (('request_id', 'request_id'), ('network_id', 'network_id')),
        'native': (('requestId', 'network_id'), ('network_id', 'network_id'))}
    if (type(event_kind) is not str or event_kind not in relations
            or type(record) is not dict or type(errors) not in (list, tuple)):
        return True
    pairs = relations[event_kind]
    scope_fields = ('cdp_scope_id', 'page_target_id')
    if any(type(record.get(name)) is not str or not record[name]
            for name in (*scope_fields, *(left for left, _ in pairs))):
        return True
    for error in errors:
        if type(error) is not dict or any(type(error.get(name)) is not str or not error[name]
                for name in (*scope_fields, 'request_id', 'network_id')):
            return True
        if any(record[left] == error[right] for left, right in pairs):
            return True
    return False


def gate_task_errors(errors):
    # A false flag is never authority. Replay the complete native proof at every
    # gate; external, incomplete, changed or unknown records remain blocking.
    return [value for value in errors if not _verified_canceled_read_proof(value)]


def _verified_canceled_read_proof(value):
    if (value.get('gate_failure') is not False or value.get('classification') != 'confirmed_canceled_read'
            or value.get('original_classification') != 'invalid_interception_id'
            or value.get('request_cancellation_confirmed') is not True or value.get('request_success_claimed') is not False):
        return False
    try:
        replay = deepcopy(value)
        replay.update({'classification': 'invalid_interception_id', 'gate_failure': True,
                       'error': value['original_interception_exception']})
        authoritative_paused = value['authoritative_paused_records']
        authoritative_native = value['authoritative_native_records']
        if not isinstance(authoritative_paused, list) or not isinstance(authoritative_native, list):
            return False
        reconcile_canceled_read_errors([replay], authoritative_paused,
            authoritative_native, value['allowed_local_origins'], request_starts=value.get('authoritative_start_records', []))
        return (replay.get('gate_failure') is False and replay.get('classification') == 'confirmed_canceled_read'
                and replay.get('fetch_request_paused') == value.get('fetch_request_paused')
                and replay.get('native_loading_failed') == value.get('native_loading_failed')
                and replay.get('derived_native_scope') == value.get('derived_native_scope')
                and replay.get('request_start_proof') == value.get('request_start_proof'))
    except (KeyError, TypeError, ValueError):
        return False


def _bound_local_read(request, origins):
    parsed = urlsplit(request.get('url', ''))
    origin = urlunsplit((parsed.scheme, parsed.netloc, '', '', ''))
    if (request.get('method') not in {'GET', 'HEAD'} or origin not in origins
            or parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'}
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        return False
    session, run = request.get('session_id'), request.get('run_id')
    allowed = {'/api/health', '/api/sessions'}
    if session:
        allowed.update(f'/api/sessions/{session}{suffix}' for suffix in ('', '/messages', '/runs'))
    if run:
        allowed.update(f'/api/runs/{run}{suffix}' for suffix in ('', '/events'))
    for artifact in request.get('artifact_ids', []):
        allowed.update(f'/api/artifacts/{artifact}{suffix}' for suffix in ('', '/content'))
    return parsed.path in allowed


def _retain_ledger_records(previous, incoming):
    """Keep every known fact and duplicate multiplicity; never silently dedup."""
    key = lambda record: json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False)
    retained = deepcopy(previous)
    old_counts, seen = Counter(key(record) for record in previous), Counter()
    for record in incoming:
        identity = key(record)
        seen[identity] += 1
        if seen[identity] > old_counts[identity]:
            retained.append(deepcopy(record))
    return retained


def _native_start_identity_valid(start, *, allow_redirect=False):
    fields = ('request_id', 'raw_url_sha256', 'method', 'frame_id', 'loader_id', 'resource_type', 'redirect_hop')
    packet = start.get('native_identity')
    return (isinstance(packet, dict) and all(name in packet and packet[name] == start.get(name) for name in fields)
        and all(isinstance(start.get(name), str) and start[name] for name in fields[:-1])
        and (type(start.get('redirect_hop')) is bool if allow_redirect else start.get('redirect_hop') is False)
        and bool(re.fullmatch(r'[0-9a-f]{64}', start['raw_url_sha256'])))


def _cancel_native_graph(value):
    """Inspect exact native values without executing unknown Python hooks.

    Shared read-only containers are supported; cycles and unknown values are not.
    This is a support-domain probe, never a replacement proof or serialization.
    """
    containers, active = set(), set()
    def visit(item):
        kind = type(item)
        if item is None or kind is str or kind is bool or kind is int:
            return True
        if kind is float:
            return math.isfinite(item)
        if kind is not dict and kind is not list and kind is not tuple:
            return False
        identity = id(item)
        if identity in active:
            return False
        if identity in containers:
            return True
        if kind is dict and any(type(key) is not str for key in item):
            return False
        containers.add(identity); active.add(identity)
        children = item.values() if kind is dict else item
        valid = all(visit(child) for child in children)
        active.remove(identity)
        return valid
    try:
        return containers if visit(value) else None
    except RecursionError:
        return None


def _cancel_relation_index(errors, paused_requests, loading_failures, origins, request_starts):
    """Fresh invocation-local physical positions, or whole-call original scans.

    Called lazily at the original first selector boundary. Unsupported inputs are
    not consumed, normalized or converted into errors by this optimization.
    """
    sources = (paused_requests, loading_failures, request_starts)
    if any(type(value) is not list and type(value) is not tuple for value in (errors, origins, *sources)):
        return None
    if _cancel_native_graph((errors, origins, *sources)) is None:
        return None
    if any(type(error) is not dict for error in errors):
        return None
    if len({id(error) for error in errors}) != len(errors):
        return None
    readonly = _cancel_native_graph((origins, *sources))
    if readonly is None:
        return None
    # Original updates/setdefaults and decision-history appends must not mutate
    # any selection-visible input through an alias. Untouched artifact_ids shared
    # lists alone do not cause fallback. All checks below are now hook-free.
    for error in errors:
        history = error.get('cancellation_reconciliation_history')
        if id(error) in readonly or (type(history) is list and (id(history) in readonly or history is errors)):
            return None
    fields = ('cdp_scope_id', 'page_target_id', 'request_id', 'network_id', 'requestId')
    for rows in (errors, *sources):
        for row in rows:
            if type(row) is not dict:
                return None
            for field in fields:
                value = row.get(field)
                if value is not None and type(value) is not str:
                    return None
    indexes = []
    for rows, aliases in zip(sources, (('network_id', 'request_id'), ('requestId', 'network_id'), ('request_id',))):
        tables = [{} for _ in aliases]
        for position, row in enumerate(rows):
            scope = (row.get('cdp_scope_id'), row.get('page_target_id'))
            for alias, table in zip(aliases, tables):
                table.setdefault((*scope, row.get(alias)), []).append(position)
        indexes.append(tables)
    return indexes


def _cancel_relation_candidates(index, kind, rows, error):
    if index is None:
        return rows
    scope = (error.get('cdp_scope_id'), error.get('page_target_id'))
    query_fields = (('network_id', 'request_id'), ('network_id', 'network_id'), ('network_id',))[kind]
    positions = set()
    for field, table in zip(query_fields, index[kind]):
        positions.update(table.get((*scope, error.get(field)), ()))
    # Union only positions, not row identities/equality. Resolve live rows and
    # leave all original same_context/OR proof predicates below unchanged.
    return (rows[position] for position in sorted(positions))


def reconcile_canceled_read_errors(errors, paused_requests, loading_failures, origins, *, request_starts=()):
    """Revalidate every cancellation against the full retained related ledgers."""
    scope_fields = ('case_id', 'case_epoch', 'cdp_scope_id', 'page_target_id')
    binding_fields = (*scope_fields, 'request_id', 'network_id', 'session_id', 'run_id', 'artifact_ids',
        'method', 'url', 'raw_url_sha256', 'frame_id', 'operation')
    relation_index, index_checked = None, False
    for error in errors:
        if error.get('classification') not in {'invalid_interception_id', 'confirmed_canceled_read'} or error.get('task_kind') != 'request_guard':
            continue
        was_confirmed = error.get('classification') == 'confirmed_canceled_read'
        previous_proof = {'fetch_request_paused': deepcopy(error.get('fetch_request_paused')),
            'native_loading_failed': deepcopy(error.get('native_loading_failed'))} if was_confirmed else None
        error.setdefault('original_interception_exception', error.get('error', ''))
        error.setdefault('original_classification', 'invalid_interception_id')
        error.update({'classification': 'invalid_interception_id', 'request_cancellation_confirmed': False,
                      'gate_failure': True, 'request_success_claimed': False, 'allowed_local_origins': list(origins)})
        def same_context(value):
            return value.get('cdp_scope_id') == error.get('cdp_scope_id') and value.get('page_target_id') == error.get('page_target_id')
        if not index_checked:
            relation_index = _cancel_relation_index(errors, paused_requests, loading_failures, origins, request_starts)
            index_checked = True
        current_paused = [value for value in _cancel_relation_candidates(relation_index, 0, paused_requests, error) if same_context(value)
            and (value.get('network_id') == error.get('network_id') or value.get('request_id') == error.get('request_id'))]
        current_native = [value for value in _cancel_relation_candidates(relation_index, 1, loading_failures, error) if same_context(value)
            and (value.get('requestId') == error.get('network_id') or value.get('network_id') == error.get('network_id'))]
        current_starts = [value for value in _cancel_relation_candidates(relation_index, 2, request_starts, error) if same_context(value) and value.get('request_id') == error.get('network_id')]
        retained_paused = _retain_ledger_records(error.get('authoritative_paused_records', []), current_paused)
        retained_native = _retain_ledger_records(error.get('authoritative_native_records', []), current_native)
        retained_starts = _retain_ledger_records(error.get('authoritative_start_records', []), current_starts)
        error.update({'authoritative_paused_records': retained_paused, 'authoritative_native_records': retained_native,
            'authoritative_start_records': retained_starts,
            'authority_basis': 'all retained same-CDP/page records related by network ID or Fetch ID; case conflicts retained'})

        def decision(state, reason):
            evidence = {'state': state, 'reason': reason, 'paused_records': deepcopy(retained_paused),
                'native_records': deepcopy(retained_native), 'start_records': deepcopy(retained_starts)}
            history = error.setdefault('cancellation_reconciliation_history', [])
            if not history or history[-1] != evidence:
                history.append(evidence)
            if was_confirmed and state != 'confirmed':
                error['cancellation_confirmation_revoked'] = True
                error['cancellation_revocation_evidence'] = {'prior_confirmation': previous_proof, **deepcopy(evidence)}

        if ('Invalid InterceptionId' not in error.get('error', '') or 'Fetch.continueRequest' not in error.get('error', '')
                or error.get('operation') != 'Fetch.continueRequest' or not _bound_local_read(error, origins)
                or any(not isinstance(error.get(name), str) or not error[name] for name in (*scope_fields, 'request_id', 'network_id'))):
            decision('confirmation_revoked' if was_confirmed else 'blocking', 'missing or invalid exact original read binding')
            continue
        paused = [value for value in retained_paused if all(value.get(name) == error.get(name) for name in binding_fields)]
        failed = [value for value in retained_native if value.get('requestId') == error['network_id']
            and value.get('network_id') == error['network_id'] and (all(value.get(name) == error[name] for name in scope_fields)
                or (value.get('case_id') is None and value.get('case_epoch') is None))]
        if len(paused) != 1 or len(retained_paused) != 1 or len(failed) != 1 or len(retained_native) != 1:
            decision('confirmation_revoked' if was_confirmed else 'blocking', 'full retained Fetch/native relation is missing, ambiguous or crosses case scope')
            continue
        native = failed[0]
        needs_derived_scope = native.get('case_id') is None and native.get('case_epoch') is None
        if needs_derived_scope or retained_starts:
            start = retained_starts[0] if len(retained_starts) == 1 else {}
            start_fields = (*scope_fields, 'session_id', 'run_id', 'artifact_ids', 'method', 'url', 'raw_url_sha256', 'frame_id')
            if (not _native_start_identity_valid(start) or start.get('resource_type') != 'Fetch'
                    or any(start.get(name) != error.get(name) for name in start_fields)
                    or start.get('request_id') != error['network_id'] or len(retained_starts) != 1
                    or type(start.get('received_sequence')) is not int or type(native.get('received_sequence')) is not int
                    or not start['received_sequence'] < native['received_sequence']):
                decision('confirmation_revoked' if was_confirmed else 'blocking', 'independent native request-start identity/scope/phase is absent or ambiguous')
                continue
            error['request_start_proof'] = deepcopy(start)
            if needs_derived_scope:
                error['derived_native_scope'] = {name: start[name] for name in scope_fields}
        raw_native = native.get('native_event')
        if (not isinstance(raw_native, dict) or any(name not in raw_native for name in ('requestId', 'canceled', 'errorText', 'type'))
                or any(raw_native[name] != native.get(name) for name in ('requestId', 'canceled', 'errorText', 'type'))
                or raw_native.get('canceled') is not True or raw_native.get('blockedReason') is not None
                or raw_native.get('corsErrorStatus') is not None
                or native.get('cors_status_field_present') is not ('corsErrorStatus' in raw_native)
                or native.get('blocked_reason_field_present') is not ('blockedReason' in raw_native)):
            decision('confirmation_revoked' if was_confirmed else 'blocking', 'native packet or explicit optional-field presence does not prove cancellation')
            continue
        if (native.get('canceled') is not True or native.get('errorText') != 'net::ERR_ABORTED'
                or native.get('type') != 'Fetch' or native.get('blockedReason') is not None
                or native.get('corsErrorStatus') is not None):
            decision('confirmation_revoked' if was_confirmed else 'blocking', 'native cancellation criteria were not met')
            continue
        error.update({'classification': 'confirmed_canceled_read', 'request_cancellation_confirmed': True, 'gate_failure': False,
            'fetch_request_paused': deepcopy(paused[0]), 'native_loading_failed': deepcopy(native),
            'allowed_local_origins': list(origins),
            'request_success_claimed': False,
            'proof_basis': 'same CDP/page/case epoch; exact FetchID-to-networkID native loadingFailed canceled=true ERR_ABORTED Fetch'})
        decision('confirmed', 'full retained related ledgers contain exactly one bound Fetch and one confirming Native packet')


def finalize_background_errors(records, errors):
    for record in records:
        relevant = [value for value in errors if value.get('case_id') in (None, record['case_id'])]
        if relevant:
            record['background_task_errors'] = relevant
            if gate_task_errors(relevant):
                record['passed'] = False
                record['late_background_task_error'] = True


async def wait_for_paint_ready(measure, *, deadline, attempts, core_mask, contour_mask, allowed_mask):
    """Await the first locked real pixel pass inside the original fixed deadline.

    A stable blank canvas is preserved as not-ready evidence. It cannot become
    success through time alone; every successful measurement still passes the
    unchanged causal core/contour/local/background/outside criteria.
    """
    last_error = 'no completed actual causal paint measurement'
    retryable = ('insufficient core coverage', 'insufficient contour coverage', 'insufficient raw contour coverage',
                 'insufficient local contour coverage', 'hidden background unstable', 'shown image unstable',
                 'outside geometry noise')
    while time.monotonic() < deadline:
        images, metadata = await measure(len(attempts))
        attempt = {**metadata, 'paint_readiness': 'not_ready', 'deadline_remaining_seconds': max(0, deadline-time.monotonic())}
        attempts.append(attempt)
        if time.monotonic() >= deadline:
            attempt['error'] = 'fixed paint readiness deadline exhausted during actual capture'
            break
        try:
            oracle_started = time.monotonic()
            statistics = verify_visible_paint(*images, core_mask=core_mask, contour_mask=contour_mask, allowed_mask=allowed_mask)
            attempt.update({'statistics': statistics, 'oracle_elapsed_seconds': time.monotonic()-oracle_started,
                'deadline_remaining_seconds': max(0, deadline-time.monotonic())})
            if time.monotonic() >= deadline:
                attempt['error'] = last_error = 'fixed paint readiness deadline exhausted during locked pixel oracle'
                break
            attempt['paint_readiness'] = 'ready'
            return statistics
        except ValueError as error:
            last_error = str(error)
            attempt['error'] = safe_error(error)
            if not any(value in last_error for value in retryable):
                raise
    raise ValueError('fixed paint readiness deadline: ' + last_error)


def sdk_conditional_headers(headers):
    safe = {name.lower(): str(value) for name, value in headers.items()}
    authority = [safe[name] for name in (':authority', 'host') if safe.get(name)]
    return {'if_none_match_present': bool(safe.get('if-none-match')),
        'if_none_match_sha256': hashlib.sha256(safe['if-none-match'].encode()).hexdigest() if safe.get('if-none-match') else None,
        'etag_sha256': hashlib.sha256(safe['etag'].encode()).hexdigest() if safe.get('etag') else None,
        'last_modified_present': bool(safe.get('last-modified')),
        'last_modified_sha256': hashlib.sha256(safe['last-modified'].encode()).hexdigest() if safe.get('last-modified') else None,
        'location_value_sha256': hashlib.sha256(safe['location'].encode()).hexdigest() if safe.get('location') else None,
        'request_authority_sha256': hashlib.sha256(authority[0].encode()).hexdigest() if len(authority) == 1 else None}


def capture_native_completion(event, *, cdp_scope_id, page_target_id, received_sequence, request_starts):
    # loadingFinished does not provide frame/loader/case fields. Keep its actual
    # packet separate from the immutable request-start candidates that bind it.
    return {'request_id': event['requestId'], 'cdp_scope_id': cdp_scope_id, 'page_target_id': page_target_id,
        'received_sequence': received_sequence, 'native_timestamp': event.get('timestamp'),
        'encoded_data_length': event.get('encodedDataLength'), 'native_event': deepcopy(event),
        'request_start_candidates': deepcopy([value for value in request_starts
            if value.get('request_id') == event['requestId'] and value.get('cdp_scope_id') == cdp_scope_id
            and value.get('page_target_id') == page_target_id])}


def native_completion_verified(completion, start, *, source_candidates=None):
    packet = completion.get('native_event')
    numeric = lambda value: type(value) in (int, float) and math.isfinite(value)
    return (isinstance(packet, dict) and packet.get('requestId') == completion.get('request_id') == start.get('request_id')
        and all(completion.get(name) == start.get(name) and isinstance(start.get(name), str) and start[name]
            for name in ('cdp_scope_id', 'page_target_id'))
        and numeric(packet.get('timestamp')) and packet['timestamp'] == completion.get('native_timestamp')
        and numeric(packet.get('encodedDataLength')) and packet['encodedDataLength'] >= 0
        and packet['encodedDataLength'] == completion.get('encoded_data_length')
        and completion.get('request_start_candidates') == (source_candidates if source_candidates is not None else [start])
        and type(completion.get('received_sequence')) is int)


def native_wire_response_verified(wire):
    packet = wire.get('native_identity')
    return (isinstance(packet, dict) and all(name in packet and packet[name] == wire.get(name)
        for name in ('request_id', 'status_code', 'etag_sha256'))
        and packet.get('location_value_sha256') == wire.get('location_value_sha256')
        and isinstance(wire.get('request_id'), str) and bool(wire['request_id'])
        and type(wire.get('status_code')) is int)


def native_request_extra_verified(extra):
    present, validator = extra.get('if_none_match_present'), extra.get('if_none_match_sha256')
    return type(present) is bool and (validator is None if present is False else
        isinstance(validator, str) and bool(re.fullmatch(r'[0-9a-f]{64}', validator)))


def sdk_authority_related(value, ids, scope, name):
    identities = {value.get('request_id')}
    if name == 'finished':
        identities.add(value.get('native_event', {}).get('requestId'))
    if not identities.intersection(ids):
        return False
    candidates = [value]
    if name == 'finished':
        candidates += value.get('request_start_candidates', [])
    return any(item.get('cdp_scope_id') == scope.get('cdp_scope_id')
        and item.get('page_target_id') == scope.get('page_target_id') for item in candidates)


def sdk_authority_snapshot(witness, scope, ids):
    return {name: deepcopy([value for value in witness.get(name, []) if sdk_authority_related(value, ids, scope, name)])
        for name in ('starts', 'responses', 'bodies', 'extra_infos', 'response_extra_infos', 'finished', 'prior_receipts')}


def sdk_raw_chain_shape_verified(chain, current_hash):
    return (isinstance(chain, list) and len(chain) in (1, 2)
        and all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) for value in chain)
        and chain[-1] == current_hash and (len(chain) == 1 or chain[0] != chain[1]))


def ordinary_two_hop_source(witness, resource):
    """Only the fully observed ordinary two-hop302->200 profile; never cache304."""
    def require(condition):
        if not condition:
            raise ValueError('SDK ordinary two-hop source is missing, contradictory or ambiguous')
    chain = resource.get('redirect_chain_raw_url_sha256')
    require(isinstance(chain, list) and len(chain) == 2 and len(set(chain)) == 2
        and all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) for value in chain))
    selected = sdk_authority_snapshot(witness, resource, {resource['request_id']})
    require(len(selected['starts']) == len(selected['extra_infos']) == len(selected['response_extra_infos']) == 2
        and len(selected['responses']) == len(selected['finished']) == 1)
    starts = []
    fields = ('request_id', 'case_id', 'case_epoch', 'cdp_scope_id', 'page_target_id', 'frame_id', 'loader_id')
    for raw_hash in chain:
        matches = [value for value in selected['starts'] if value.get('raw_url_sha256') == raw_hash]
        require(len(matches) == 1)
        start = matches[0]
        require(_native_start_identity_valid(start, allow_redirect=True) and start.get('method') == 'GET'
            and start.get('resource_type') == 'Script' and all(start.get(name) == resource.get(name)
                and isinstance(resource.get(name), str) and resource[name] for name in fields))
        starts.append(start)
    initial, final = starts
    require(initial['redirect_hop'] is False and final['redirect_hop'] is True and chain[1] == resource.get('raw_url_sha256'))
    link = final.get('redirect_source')
    require(isinstance(link, dict) and final['native_identity'].get('redirect_source') == link
        and link.get('previous_raw_url_sha256') == chain[0] and link.get('status') == 302
        and link.get('resolved_location_raw_url_sha256') == chain[1]
        and isinstance(link.get('location_value_sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', link['location_value_sha256']))
    wires = []
    for status in (302, 200):
        matches = [value for value in selected['response_extra_infos'] if value.get('status_code') == status]
        require(len(matches) == 1 and native_wire_response_verified(matches[0]))
        wires.append(matches[0])
    require(wires[0].get('location_value_sha256') == link['location_value_sha256'])
    require(initial.get('request_authority_sha256') != final.get('request_authority_sha256'))
    for start in starts:
        authority = start.get('request_authority_sha256')
        matches = [value for value in selected['extra_infos'] if value.get('request_authority_sha256') == authority]
        require(isinstance(authority, str) and re.fullmatch(r'[0-9a-f]{64}', authority)
            and len(matches) == 1 and native_request_extra_verified(matches[0]))
    response, completion = selected['responses'][0], selected['finished'][0]
    require(response.get('status') == 200 and response.get('resource_type') == 'Script'
        and response.get('raw_url_sha256') == chain[1] and all(response.get(name) == resource[name] for name in fields)
        and native_completion_verified(completion, final, source_candidates=starts))
    require(all(type(value.get('received_sequence')) is int for value in (*starts, response, completion))
        and initial['received_sequence'] < final['received_sequence'] < response['received_sequence'] < completion['received_sequence'])
    return {'starts': starts, 'response': response, 'finished': completion,
        'request_extra_infos': selected['extra_infos'], 'wire_response_extra_infos': wires}


def verify_sdk_cache_representation(witness, *, scope, raw_url_sha256, actual_bytes, actual_representation=None):
    """Authenticate fresh current CDP bytes, not wire304 or a cached false flag."""
    def require(condition, reason):
        if not condition:
            raise ValueError('SDK cache: ' + reason)
    require(isinstance(witness, dict) and all(isinstance(witness.get(name), list)
        and all(isinstance(value, dict) for value in witness[name])
        for name in ('starts', 'responses', 'bodies', 'extra_infos', 'response_extra_infos', 'finished', 'prior_receipts')), 'unknown native ledger shape')
    if actual_bytes is not None:
        require(isinstance(actual_bytes, bytes) and bool(actual_bytes), 'missing fresh nonempty actual bytes')
        actual_sha, actual_size = hashlib.sha256(actual_bytes).hexdigest(), len(actual_bytes)
    else:
        # Final replay rechecks retained actual CDP metadata, not fabricated bytes.
        require(isinstance(actual_representation, dict), 'missing actual representation receipt')
        actual_sha, actual_size = actual_representation.get('sha256'), actual_representation.get('size_bytes')
        require(isinstance(actual_sha, str) and re.fullmatch(r'[0-9a-f]{64}', actual_sha)
            and type(actual_size) is int and actual_size > 0, 'unreadable actual representation receipt')
    fields = ('case_id', 'case_epoch', 'cdp_scope_id', 'page_target_id', 'frame_id', 'loader_id')
    current_id = scope.get('request_id')
    require(isinstance(current_id, str) and bool(current_id), 'unknown current native request ID')
    def one(name, request_id):
        candidates = [value for value in witness.get(name, []) if sdk_authority_related(value, {request_id}, scope, name)]
        require(len(candidates) == 1, 'missing or ambiguous ' + name)
        return candidates[0]
    start, response, body, extra, wire, completion = (one(name, current_id) for name in
        ('starts', 'responses', 'bodies', 'extra_infos', 'response_extra_infos', 'finished'))
    require(_native_start_identity_valid(start), 'unreadable or redirected start')
    require(all(isinstance(scope.get(name), str) and scope[name] and start.get(name) == scope[name] for name in fields), 'current scope mismatch')
    require(start.get('method') == 'GET' and start.get('resource_type') == 'Script', 'not actual SDK script GET')
    for value in (start, response, body):
        require(value.get('raw_url_sha256') == raw_url_sha256 and all(value.get(name) == scope[name] for name in fields), 'current native identity mismatch')
    require(response.get('status') in (200, 304) and response.get('resource_type') == 'Script'
        and native_wire_response_verified(wire) and wire.get('status_code') == 304, 'wire response not actual304 script')
    require(native_completion_verified(completion, start) and body.get('completion_proof') == completion, 'current completion/source unproved')
    require(body.get('operation') == 'Network.getResponseBody' and body.get('success') is True
        and type(body.get('size_bytes')) is int and body['size_bytes'] == actual_size and body.get('sha256') == actual_sha, 'current actual CDP body mismatch')
    validator = extra.get('if_none_match_sha256')
    require(extra.get('if_none_match_present') is True and isinstance(validator, str)
        and re.fullmatch(r'[0-9a-f]{64}', validator) and validator == response.get('etag_sha256') == wire.get('etag_sha256'), 'missing/conflicting conditional validator')
    prior_candidates = [value for value in witness.get('prior_receipts', []) if value.get('raw_url_sha256') == raw_url_sha256
        and value.get('cdp_scope_id') == scope['cdp_scope_id'] and value.get('page_target_id') == scope['page_target_id']
        and value.get('status') == 200 and value.get('kind') == 'loaded']
    require(type(start.get('received_sequence')) is int, 'current start sequence is unproved')
    predecessors = []
    for candidate in prior_candidates:
        candidate_id = candidate.get('request_id')
        require(isinstance(candidate_id, str) and candidate_id and candidate_id != current_id,
            'unknown prior native request ID')
        candidate_chain = candidate.get('redirect_chain_raw_url_sha256')
        if isinstance(candidate_chain, list) and len(candidate_chain) == 2:
            source = ordinary_two_hop_source(witness, candidate)
            require(all(value['received_sequence'] > start['received_sequence']
                for value in source['starts']), 'prior redirect is not provably later than current start')
            continue
        candidate_start = one('starts', candidate_id)
        require(_native_start_identity_valid(candidate_start)
            and candidate_start.get('raw_url_sha256') == raw_url_sha256
            and all(candidate_start.get(name) == candidate.get(name)
                and isinstance(candidate.get(name), str) and candidate[name] for name in fields)
            and type(candidate_start.get('received_sequence')) is int, 'prior start identity/sequence is unproved')
        # A later native start cannot have supplied the current cached bytes.
        # Array order, cached validators and body equality do not choose a source.
        if candidate_start['received_sequence'] > start['received_sequence']:
            continue
        require(candidate_start['received_sequence'] < start['received_sequence'],
            'ambiguous prior/current start sequence')
        candidate_body = one('bodies', candidate_id)
        require(candidate_body.get('raw_url_sha256') == raw_url_sha256
            and all(candidate_body.get(name) == candidate.get(name) for name in fields)
            and type(candidate_body.get('received_sequence')) is int
            and candidate_start['received_sequence'] < candidate_body['received_sequence'] < start['received_sequence'],
            'prior body/current start ordering is unproved')
        predecessors.append((candidate_body['received_sequence'], candidate))
    require(bool(predecessors), 'missing or ambiguous prior real200 receipt')
    latest_sequence = max(sequence for sequence, _ in predecessors)
    latest = [candidate for sequence, candidate in predecessors if sequence == latest_sequence]
    require(len(latest) == 1, 'missing or ambiguous prior real200 receipt')
    prior = latest[0]
    prior_id = prior.get('request_id')
    require(prior_id != current_id and isinstance(prior_id, str) and bool(prior_id), 'unknown prior native request ID')
    old_start, old_response, old_body, old_wire, old_completion, old_extra = (one(name, prior_id) for name in
        ('starts', 'responses', 'bodies', 'response_extra_infos', 'finished', 'extra_infos'))
    require(_native_start_identity_valid(old_start), 'prior redirect or unknown start')
    require(old_start.get('method') == 'GET' and old_start.get('resource_type') == 'Script'
        and old_response.get('status') == 200 and old_response.get('resource_type') == 'Script'
        and native_wire_response_verified(old_wire) and old_wire.get('status_code') == 200, 'prior is not native wire200 script')
    for value in (old_start, old_response, old_body):
        require(value.get('raw_url_sha256') == raw_url_sha256 and all(value.get(name) == prior.get(name)
            and isinstance(prior.get(name), str) and prior[name] for name in fields), 'prior native identity mismatch')
    require(old_response.get('etag_sha256') == old_wire.get('etag_sha256') == validator, 'prior validator mismatch')
    require(native_request_extra_verified(old_extra), 'prior actual request ExtraInfo unproved')
    require(native_completion_verified(old_completion, old_start) and old_body.get('completion_proof') == old_completion, 'prior completion/source unproved')
    require(old_body.get('success') is True and old_body.get('operation') == 'Network.getResponseBody'
        and old_body.get('sha256') == prior.get('sha256') == actual_sha
        and type(old_body.get('size_bytes')) is int and type(prior.get('size_bytes')) is int
        and old_body['size_bytes'] == prior.get('size_bytes') == actual_size, 'prior/current representation differs')
    require(all(type(value.get('received_sequence')) is int for value in
        (old_start, old_response, old_completion, old_body, start, response, completion, body))
        and old_start['received_sequence'] < old_response['received_sequence'] < old_completion['received_sequence'] < old_body['received_sequence'] < start['received_sequence']
        and start['received_sequence'] < response['received_sequence'] < completion['received_sequence'] < body['received_sequence'], 'real prior200/current native receipt phase is unproved')
    return {'current_request_id': current_id, 'prior_request_id': prior_id, 'raw_url_sha256': raw_url_sha256,
        'wire_status': 304, 'effective_status': response['status'], 'actual_body_sha256': actual_sha, 'actual_body_size_bytes': actual_size,
        'starts': deepcopy([old_start, start]), 'responses': deepcopy([old_response, response]),
        'bodies': deepcopy([old_body, body]), 'extra_infos': deepcopy([old_extra, extra]), 'prior_receipts': deepcopy([prior]),
        'response_extra_infos': deepcopy([old_wire, wire]), 'finished': deepcopy([old_completion, completion])}


def sdk_resource_identity_verified(resource, witness):
    try:
        chain = resource.get('redirect_chain_raw_url_sha256')
        if not sdk_raw_chain_shape_verified(chain, resource.get('raw_url_sha256')):
            return False
        if resource.get('status') == 304 and resource.get('kind') == 'cache_served_representation':
            if chain != [resource.get('raw_url_sha256')]:
                return False
            proof = verify_sdk_cache_representation(witness, scope=resource,
                raw_url_sha256=resource['raw_url_sha256'], actual_bytes=None, actual_representation=resource)
            return proof == resource.get('native_cache_proof')
        if resource.get('status') != 200 or resource.get('kind') != 'loaded':
            return False
        if len(resource.get('redirect_chain_raw_url_sha256', [])) == 2:
            source = ordinary_two_hop_source(witness, resource)
            bodies = sdk_authority_snapshot(witness, resource, {resource['request_id']})['bodies']
            if len(bodies) != 1:
                return False
            body = bodies[0]
            return (body.get('success') is True and body.get('operation') == 'Network.getResponseBody'
                and body.get('completion_proof') == source['finished']
                and all(body.get(name) == resource.get(name) for name in
                    ('request_id', 'raw_url_sha256', 'case_id', 'case_epoch', 'cdp_scope_id', 'page_target_id', 'frame_id', 'loader_id'))
                and type(body.get('size_bytes')) is int and type(resource.get('size_bytes')) is int
                and body['size_bytes'] == resource['size_bytes'] > 0 and body.get('sha256') == resource.get('sha256')
                and type(body.get('received_sequence')) is int and source['finished']['received_sequence'] < body['received_sequence'])
        request_id = resource['request_id']
        fields = ('case_id', 'case_epoch', 'cdp_scope_id', 'page_target_id', 'frame_id', 'loader_id', 'raw_url_sha256')
        if any(not isinstance(resource.get(name), str) or not resource[name] for name in (*fields, 'request_id')):
            return False
        selected = {}
        for name in ('starts', 'responses', 'bodies', 'extra_infos', 'response_extra_infos', 'finished'):
            candidates = [value for value in witness.get(name, []) if sdk_authority_related(value, {request_id}, resource, name)]
            if len(candidates) != 1 or (name in ('starts', 'responses', 'bodies')
                and any(candidates[0].get(field) != resource.get(field) for field in fields)):
                return False
            selected[name] = candidates[0]
        return (selected['responses'].get('status') == 200 and selected['responses'].get('resource_type') == 'Script'
            and native_wire_response_verified(selected['response_extra_infos']) and selected['response_extra_infos'].get('status_code') == 200
            and native_request_extra_verified(selected['extra_infos'])
            and native_completion_verified(selected['finished'], selected['starts'])
            and selected['bodies'].get('completion_proof') == selected['finished']
            and selected['starts'].get('method') == 'GET' and selected['bodies'].get('success') is True
            and selected['bodies'].get('operation') == 'Network.getResponseBody'
            and selected['bodies'].get('sha256') == resource.get('sha256')
            and type(resource.get('size_bytes')) is int and resource['size_bytes'] > 0
            and type(selected['bodies'].get('size_bytes')) is int and selected['bodies'].get('size_bytes') == resource['size_bytes']
            and all(type(selected[name].get('received_sequence')) is int for name in ('starts', 'responses', 'finished', 'bodies'))
            and selected['starts']['received_sequence'] < selected['responses']['received_sequence'] < selected['finished']['received_sequence'] < selected['bodies']['received_sequence'])
    except (KeyError, TypeError, ValueError):
        return False


class SdkVerificationPass:
    """One synchronous fixed-witness pass, never a mutable cross-call green cache.

    The index stores raw positions, retains duplicates and independently crosses
    every finished alias ID with every candidate scope. The unchanged predicate
    consumes original-order candidates and remains the sole identity decision.
    """
    def __init__(self, witness, *, closed=False, collect_positions=False):
        self.witness = witness
        self.closed = closed is True
        self.memo = {}
        self.positions = []
        self.collect_positions = collect_positions is True
        self.predicate_calls = 0
        self.index = None
        self.priors = {}
        self.drift = None
        self.resource_drift = []
        self.before = None
        try:
            self.before = strict_json_bytes(witness)
            if type(witness) is not dict or any(type(witness.get(name)) is not list
                    or any(type(value) is not dict for value in witness[name]) for name in SDK_LEDGER_NAMES):
                return
            index = {name: {} for name in SDK_LEDGER_NAMES}
            for name in SDK_LEDGER_NAMES:
                for position, value in enumerate(witness[name]):
                    ids = {value.get('request_id')}
                    scopes = [value]
                    if name == 'finished':
                        packet, candidates = value.get('native_event', {}), value.get('request_start_candidates', [])
                        if type(packet) is not dict or type(candidates) is not list or any(type(item) is not dict for item in candidates):
                            return
                        ids.add(packet.get('requestId'))
                        scopes += candidates
                    for item in scopes:
                        scope_key = (item.get('cdp_scope_id'), item.get('page_target_id'))
                        for request_id in ids:
                            # Unknown aliases never justify dropping original facts.
                            if any(type(part) not in (str, type(None)) for part in (*scope_key, request_id)):
                                return
                            index[name].setdefault((*scope_key, request_id), set()).add(position)
                    if name == 'prior_receipts':
                        key = (value.get('raw_url_sha256'), value.get('cdp_scope_id'), value.get('page_target_id'))
                        if any(type(part) not in (str, type(None)) for part in key):
                            return
                        self.priors.setdefault(key, []).append(position)
            self.index = index
        except (TypeError, ValueError, RecursionError):
            # Unknown shape uses the original complete scan, or fails closed.
            self.index = None

    def selected(self, resource):
        if self.index is None or type(resource) is not dict:
            return self.witness, {name: list(range(len(self.witness.get(name, []))))
                for name in SDK_LEDGER_NAMES if type(self.witness) is dict and type(self.witness.get(name)) is list}
        scope = (resource.get('cdp_scope_id'), resource.get('page_target_id'))
        current = resource.get('request_id')
        if any(type(part) is not str or not part for part in (*scope, current)):
            return self.witness, {}
        ids = {current}
        prior_positions = []
        if resource.get('status') == 304:
            key = (resource.get('raw_url_sha256'), *scope)
            if type(key[0]) is not str:
                return self.witness, {}
            # All same-URL/page priors, including unknown IDs, survive original
            # status/kind filtering; never select only cached proof.prior_id.
            prior_positions = self.priors.get(key, [])
            for position in prior_positions:
                request_id = self.witness['prior_receipts'][position].get('request_id')
                if type(request_id) not in (str, type(None)):
                    return self.witness, {}
                ids.add(request_id)
        selected, positions = {}, {}
        for name in SDK_LEDGER_NAMES:
            candidates = set()
            for request_id in ids:
                candidates.update(self.index[name].get((*scope, request_id), ()))
            ordered = [position for position in sorted(candidates) if sdk_authority_related(
                self.witness[name][position], ids, resource, name)]
            if name == 'prior_receipts':
                ordered = sorted(set(ordered).union(prior_positions))
            positions[name] = ordered
            selected[name] = [self.witness[name][position] for position in ordered]
        return selected, positions

    def verify(self, resource):
        try:
            # Only usage is non-predicate. Preserve typed complete payload bytes
            # as key and compare bytes, not Python bool/int equality or body SHA.
            before_resource = strict_json_bytes(resource)
            if type(resource) is not dict:
                return False
            payload = {key: value for key, value in resource.items() if key not in SDK_USAGE_FIELDS}
            encoded = strict_json_bytes(payload)
            if self.closed and encoded in self.memo and not self.collect_positions:
                return self.memo[encoded]
            selected, positions = self.selected(resource)
            if self.collect_positions:
                self.positions.append(positions)
            if self.closed and encoded in self.memo:
                return self.memo[encoded]
            self.predicate_calls += 1
            result = sdk_resource_identity_verified(resource, selected) is True
            after_resource = strict_json_bytes(resource)
            if before_resource != after_resource:
                self.resource_drift.append({'reason': 'complete receipt changed during original predicate',
                    'before': json.loads(before_resource), 'after': json.loads(after_resource)})
                result = False
            if self.closed:
                self.memo[encoded] = result
            return result
        except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
            return False

    def finish(self):
        try:
            after = strict_json_bytes(self.witness)
        except (TypeError, ValueError, RecursionError) as error:
            self.drift = {'reason': 'unknown final witness shape', 'error': safe_error(error),
                'before': json.loads(self.before) if self.before is not None else None,
                'after': deepcopy(self.witness)}
            return False
        if self.before is None or after != self.before:
            self.drift = {'reason': 'complete synchronous authority changed; all provisional results revoked',
                'before': json.loads(self.before) if self.before is not None else None, 'after': json.loads(after)}
            return False
        if self.resource_drift:
            self.drift = {'reason': 'SDK resource payload changed; all provisional results revoked',
                'resource_drift': self.resource_drift}
            return False
        return True


def verify_sdk_resources_indexed(resources, witness, *, closed=False, collect_positions=False):
    verification = SdkVerificationPass(witness, closed=closed, collect_positions=collect_positions)
    results = [verification.verify(value) for value in resources]
    stable = verification.finish()
    return {'results': results if stable else [False for _ in results],
        'selected_positions': verification.positions, 'witness_drift': verification.drift,
        'resource_drift': verification.resource_drift,
        'predicate_calls': verification.predicate_calls, 'indexed': verification.index is not None}


def apply_sdk_resource_result(record, verified, witness):
    resources = record.get('sdk_resources', [])
    was_verified = record.get('sdk_resource_native_identity_verified') is True
    record['sdk_resource_native_identity_verified'] = verified
    if not verified:
        record['passed'] = False
        if was_verified:
            record['sdk_resource_identity_revoked'] = True
            evidence = []
            for value in resources:
                ids = {value.get('request_id'), value.get('native_cache_proof', {}).get('prior_request_id')}
                authority = {}
                for name in SDK_LEDGER_NAMES:
                    retained = witness.get(name, [])
                    try:
                        authority[name] = deepcopy([item for item in retained if sdk_authority_related(item, ids, value, name)])
                    except (AttributeError, TypeError, ValueError):
                        authority[name] = deepcopy(retained)
                evidence.append({'prior_receipt': deepcopy(value), 'authority': authority})
            record['sdk_resource_revocation_evidence'] = {'resources': evidence,
                'reason': 'complete final retained Native authority no longer verifies the prior SDK identity'}


def finalize_sdk_resources(records, witness):
    verification = SdkVerificationPass(witness, closed=True)
    for record in records:
        resources = record.get('sdk_resources', [])
        # Evaluate every receipt, including after one fails, for complete diagnostics.
        outcomes = [verification.verify(value) for value in resources]
        apply_sdk_resource_result(record, bool(resources) and all(outcomes), witness)
    if not verification.finish():
        for record in records:
            apply_sdk_resource_result(record, False, witness)
            record['sdk_authority_drift'] = verification.drift


def sdk_consumer_binding_verified(record):
    scope = record.get('sdk_resource_consumer_scope')
    fields = ('case_id', 'case_epoch', 'cdp_scope_id', 'page_target_id')
    if (type(scope) is not dict or any(type(scope.get(key)) is not str or not scope[key] for key in fields)
            or record.get('case_id') != scope['case_id'] or type(record.get('sdk_resources')) is not list):
        return False
    if (any(type(scope.get(key)) is not str or not scope[key] or scope[key] != record.get(key)
            for key in ('run_id', 'session_id')) or type(scope.get('artifact_ids')) is not list
            or not scope['artifact_ids'] or any(type(value) is not str or not value for value in scope['artifact_ids'])
            or record.get('map_artifact_id') not in scope['artifact_ids']
            or ('gpkg_artifact_id' in record and record['gpkg_artifact_id'] not in scope['artifact_ids'])):
        return False
    for value in record['sdk_resources']:
        if (type(value) is not dict or value.get('used_in_case_id') != scope['case_id']
                or value.get('used_in_case_epoch') != scope['case_epoch']
                or any(type(value.get(key)) is not str or not value[key] for key in fields)
                or any(value[key] != scope[key] for key in ('cdp_scope_id', 'page_target_id'))
                or type(value.get('inherited_from_same_cdp_context')) is not bool):
            return False
        inherited = any(value[key] != scope[key] for key in ('case_id', 'case_epoch'))
        if value['inherited_from_same_cdp_context'] is not inherited:
            return False
    return True


def finalize_durable_cases(records, root, pool, witness, errors, projections, *, closed_barrier=False):
    """After close barrier: hydrate/update one whole case, never gate a stale stub."""
    verification = SdkVerificationPass(witness, closed=closed_barrier)
    failures, completed = [], 0
    def publish(stub, record):
        compact = pool.compact_record(record)
        path = checked_output_path(root, stub['_case_record_ref']['relative_path'])
        write_json(path, compact)
        refreshed = thin_case_record(compact, root)
        stub.clear(); stub.update(refreshed)
    for stub in records:
        try:
            record = pool.expand_record(load_thin_case(stub, root))
            finalize_background_errors([record], errors)
            resources = record.get('sdk_resources', [])
            outcomes = [verification.verify(value) for value in resources]
            apply_sdk_resource_result(record, bool(resources) and all(outcomes)
                and sdk_consumer_binding_verified(record), witness)
            record['historical_session_projection_responses'] = [value for value in projections if value.get('case_id') == record['case_id']]
            record['historical_session_readonly_projection_verified'] = projection_observations_verified(
                record['historical_session_projection_responses'], record['session_id'])
            if not record['historical_session_readonly_projection_verified']:
                record['passed'] = False
            publish(stub, record)
            completed += 1
        except Exception as error:
            stub.update({'passed': False, 'sdk_resource_native_identity_verified': False,
                'historical_session_readonly_projection_verified': False, 'case_record_error': safe_error(error)})
            failures.append({'case_id': stub.get('case_id'), 'stage': 'durable_final_case', 'error': safe_error(error)})
    if not verification.finish():
        # No await here. If instrumentation/tests violate the synchronous fixed
        # witness, keep both complete raw versions and revoke every prior green.
        for stub in records:
            stub['passed'] = False
            try:
                record = pool.expand_record(load_thin_case(stub, root))
                apply_sdk_resource_result(record, False, witness)
                record['sdk_authority_drift'] = verification.drift
                publish(stub, record)
            except Exception as error:
                stub['case_record_error'] = safe_error(error)
                failures.append({'case_id': stub.get('case_id'), 'stage': 'authority_drift_revoke', 'error': safe_error(error)})
    return {'complete_case_count': completed, 'errors': failures, 'predicate_calls': verification.predicate_calls,
        'indexed': verification.index is not None, 'witness_drift': verification.drift}


def compute_sealed_sdk_equivalence(report_path, output):
    """Explicit ZERO-browser/Provider format and predicate computation on old facts.

    This does not change historical flags or count as a new map/LLM audit. The
    source stays read-only; all conversion/measurement files have a fresh root.
    """
    import ctypes
    from ctypes import wintypes
    class Memory(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
            *[(name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]]
    def memory():
        if os.name != 'nt':
            return {'unavailable': 'Windows process memory API not available'}
        counters = Memory(); counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise OSError('Windows process memory measurement failed')
        return {'working_set_bytes': counters.WorkingSetSize, 'peak_working_set_bytes': counters.PeakWorkingSetSize,
            'pagefile_usage_bytes': counters.PagefileUsage, 'peak_pagefile_usage_bytes': counters.PeakPagefileUsage}
    report_path, output = Path(report_path).resolve(), Path(output).resolve()
    if (output.exists() or not output.is_relative_to(BACKEND_ROOT / '.gismind')
            or output.is_relative_to(report_path.parent) or report_path.is_relative_to(output)):
        raise ValueError('pure computation output must be a fresh separate ignored root')
    source_digest = sha256_file(report_path)
    observer_before = {path.name: sha256_file(path) for path in (Path(__file__), TOOLING_ROOT/'test_audit_rendered_results.py')}
    application_before = source_hashes()
    started = time.monotonic(); before_memory = memory()
    report = strict_read_json(report_path)
    loaded_memory = memory()
    witness = dict(zip(SDK_LEDGER_NAMES, (report['network_request_start_observations'],
        report['native_sdk_response_observations'], report['native_sdk_actual_body_receipts'],
        report['network_request_extra_info_observations'], report['network_response_extra_info_observations'],
        report['network_loading_finished_observations'], report['native_sdk_initial_http200_receipts'])))
    unique = {}; occurrences = 0; occurrence_bytes = 0
    for record in report['records']:
        for value in record.get('sdk_resources', []):
            payload = {key: item for key, item in value.items() if key not in SDK_USAGE_FIELDS}
            encoded = strict_json_bytes(payload)
            unique.setdefault(encoded, payload); occurrences += 1; occurrence_bytes += len(encoded)
    baseline_start = time.monotonic()
    baseline = []
    for index, resource in enumerate(unique.values()):
        baseline.append(sdk_resource_identity_verified(resource, witness))
        if (index+1) % 200 == 0:
            print(json.dumps({'pure_compute_complete_original_predicates': index+1, 'total': len(unique)}), flush=True)
    baseline_seconds = time.monotonic()-baseline_start
    indexed_start = time.monotonic()
    indexed = verify_sdk_resources_indexed(unique.values(), witness, closed=True)
    indexed_seconds = time.monotonic()-indexed_start
    if indexed['results'] != baseline or indexed['witness_drift']:
        raise ValueError('actual sealed unique receipts disagree with complete original predicate')
    output.mkdir(exist_ok=False)
    pool = SdkReceiptAuthority(output)
    stubs = []
    for record in report['records']:
        compact = pool.compact_record(record)
        directory = output / record['shard'] / record['case_id']; directory.mkdir(parents=True)
        write_json(directory / 'observation.json', compact)
        stub = thin_case_record(compact, output)
        restored = pool.expand_record(load_thin_case(stub, output))
        if strict_json_bytes(restored) != strict_json_bytes(record):
            raise ValueError('actual old case payload was not exactly reconstructible')
        stubs.append(stub)
    global_refs = [pool.reference(value, index) for index, value in enumerate(report['sdk_resources'])]
    validate_evidence_export(stubs, pool, global_refs)
    converted = {**report, 'evidence_schema': COMPACT_EVIDENCE_SCHEMA, 'sdk_receipt_authority': pool.table(),
        'sdk_resources': global_refs, 'records': stubs}
    write_json(output / 'lossless-conversion.json', converted)
    after_memory = memory()
    observer_after = {name: sha256_file(TOOLING_ROOT/name) for name in observer_before}
    application_after = source_hashes()
    if sha256_file(report_path) != source_digest or observer_before != observer_after or application_before != application_after:
        raise ValueError('sealed source report or caller/application source changed during pure calculation')
    receipt = {'observer_version': OBSERVER_VERSION, 'pure_computation_only': True,
        'new_browser_runs': 0, 'new_provider_requests': 0, 'historical_flags_changed': False,
        'source_summary_sha256': source_digest, 'source_summary_bytes': report_path.stat().st_size,
        'observer_files_sha256': observer_before, 'observer_files_sha256_after': observer_after,
        'application_source_file_count': len(application_before),
        'application_source_digest': hashlib.sha256(json.dumps(application_before, sort_keys=True).encode()).hexdigest(),
        'actual_case_count_exactly_reconstructed': len(stubs), 'actual_sdk_occurrences': occurrences,
        'actual_unique_complete_payloads': len(unique), 'authority_table_count': len(pool.entries),
        'canonical_occurrence_payload_bytes': occurrence_bytes,
        'canonical_unique_payload_bytes': sum(len(value) for value in unique),
        'original_complete_predicate_pass_count': sum(baseline), 'indexed_predicate_pass_count': sum(indexed['results']),
        'complete_predicate_equivalence': indexed['results'] == baseline,
        'original_complete_predicate_seconds': baseline_seconds, 'indexed_fixed_witness_seconds': indexed_seconds,
        'indexed_predicate_calls': indexed['predicate_calls'], 'indexed': indexed['indexed'],
        'raw_ledger_counts': {key: len(value) for key, value in witness.items()},
        'converted_summary_bytes': (output/'lossless-conversion.json').stat().st_size,
        'converted_summary_sha256': sha256_file(output/'lossless-conversion.json'),
        'compact_case_bytes': sum((output/value['shard']/value['case_id']/'observation.json').stat().st_size for value in stubs),
        'sdk_sidecar_bytes': sum(path.stat().st_size for path in (output/'sdk-receipts').glob('*.json')),
        'memory_before': before_memory, 'memory_after_old_json_load': loaded_memory, 'memory_after_compute_and_conversion': after_memory,
        'elapsed_seconds': time.monotonic()-started,
        'limits': 'pure single-process historical calculation; not full1800 live RSS, not SDK body bytes reauthentication, not new LLM success'}
    write_json(output/'compute-receipt.json', receipt)
    return receipt


async def observe_sdk_script_response(*, url, status, headers, redirect_chain, body_reader, observation_scope=None, cache_witness=None):
    """Observe actual redirects without requesting their unavailable body.

    Transport-only unit controls do not stand in for a loaded real SDK. Every
    final loaded resource still requires an actual HTTP200 body SHA and size.
    """
    if not redirect_chain or redirect_chain[-1] != url:
        raise ValueError('SDK actual request redirect chain is unbound')
    received_scope = deepcopy(observation_scope or {})
    record = {**received_scope, 'url': redact_url(url), 'status': status, 'resource_type': 'script',
        'redirect_chain': [redact_url(value) for value in redirect_chain],
        'redirected_from': redact_url(redirect_chain[-2]) if len(redirect_chain) > 1 else None}
    if status in {301, 302, 303, 307, 308}:
        location = {name.lower(): value for name, value in headers.items()}.get('location')
        if not isinstance(location, str) or not location:
            raise ValueError('SDK redirect lacks actual Location header')
        record.update({'kind': 'redirect', 'redirect_target': redact_url(urljoin(url, location)),
            'location': redact_url(urljoin(url, location)), 'body_read': False, 'sha256': None,
            'size_bytes': None, 'body_unavailable_by_design': True})
        return record
    if status != 200 and not (status == 304 and cache_witness is not None):
        raise ValueError(f'SDK final response is not HTTP200: status={status}')
    body = await body_reader()
    if not isinstance(body, bytes) or not body:
        raise ValueError('SDK loaded HTTP200 response has no actual bytes')
    record.update({'kind': 'loaded', 'body_read': True, 'sha256': hashlib.sha256(body).hexdigest(), 'size_bytes': len(body)})
    if status == 304:
        if len(redirect_chain) != 1:
            raise ValueError('SDK cache: ambiguous redirect hop')
        proof = verify_sdk_cache_representation(cache_witness, scope=received_scope,
            raw_url_sha256=hashlib.sha256(url.encode()).hexdigest(), actual_bytes=body)
        record.update({'kind': 'cache_served_representation', 'native_cache_proof': proof})
    return record


def sdk_receipts_for_case(resources, redirects, scope):
    """Retain the observed owner while explicitly noting authentic cache reuse."""
    fields = ('case_id', 'case_epoch', 'cdp_scope_id', 'page_target_id')
    loaded = []
    for resource in resources:
        receipt = deepcopy(resource)
        inherited = any(receipt.get(name) != scope.get(name) for name in ('case_id', 'case_epoch'))
        receipt.update({'used_in_case_id': scope['case_id'], 'used_in_case_epoch': scope['case_epoch'],
            'inherited_from_same_cdp_context': inherited
                and all(receipt.get(name) == scope.get(name) for name in ('cdp_scope_id', 'page_target_id'))})
        loaded.append(receipt)
    return {'sdk_resources': loaded, 'sdk_redirects': [deepcopy(value) for value in redirects
        if all(value.get(name) == scope.get(name) for name in fields)]}


def assign_workers(cases, concurrency):
    assigned = [[] for _ in range(concurrency)]
    for index, shard in enumerate(sorted({case['shard'] for case in cases})):
        assigned[index % concurrency].extend(case for case in cases if case['shard'] == shard)
    return assigned


async def observe_case_without_capture_contention(case, audit_one, lock):
    """Keep the fixed paint deadline free of other cases' browser/CPU work."""
    async with lock:
        return await audit_one(case)


def safe_error(error):
    message = str(error)
    message = re.sub(r'https?://[^\s\"<>]+', lambda match: redact_url(match.group(0)), message)
    message = re.sub(r'(?i)(authorization|key|token|securityjscode|securitycode)\s*[:=]\s*[^\s,;]+', r'\1=[REDACTED]', message)
    return f'{type(error).__name__}: {message}'[:4000]


def load_original(original):
    original = Path(original).resolve()
    summary = read_json(original / 'summary.json')
    validate_original_summary(summary)
    rows = [json.loads(line) for line in (original / 'results.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    if len(rows) != 2000 or len({row.get('case_id') for row in rows}) != 2000:
        raise ValueError('original results require exactly 2000 unique case IDs')
    cases, excluded = [], []
    for row in rows:
        case_root = Path(row['evidence_path']).resolve()
        if original not in case_root.parents or case_root.parent.parent != original:
            raise ValueError('original evidence path escapes its sealed shard')
        case = bind_case(row, read_json(case_root / 'result.json'), case_root)
        if case.get('excluded'):
            excluded.append(case)
        else:
            case['user_id'] = trace_user_id(case_root / 'trace.zip')
            case['trace_sha256'] = sha256_file(case_root / 'trace.zip')
            cases.append(case)
    map_ids = [case['map_artifact']['artifact_id'] for case in cases]
    if len(map_ids) != EXPECTED_MAPS or len(set(map_ids)) != EXPECTED_MAPS or len(excluded) != EXPECTED_RASTERS:
        raise ValueError('original final scope must be 1800 unique MapLayers and 200 explicit Rasters')
    return cases, excluded


def validate_server_map(original, value):
    required = {f'shard-{i}' for i in range(1, 5)}
    if set(value) != required:
        raise ValueError('server map requires exactly the original four shard names')
    for shard, server in value.items():
        for name in ('frontend_url', 'backend_url'):
            parsed = urlsplit(server.get(name, ''))
            if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'}
                    or not parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or parsed.path not in ('', '/')):
                raise ValueError('observer server endpoints must be credential-free local HTTP origins')
        clone = Path(server['clone_root']).resolve()
        if clone == original or original in clone.parents or TOOLING_ROOT not in clone.parents:
            raise ValueError('observer clone must be outside sealed evidence under independent tooling')
        receipt_path = clone / 'observer_clone_receipt.json'
        receipt = read_json(receipt_path)
        if (receipt.get('kind') != 'independent_history_observer_clone_v1'
                or Path(receipt.get('original_shard', '')).resolve() != original / shard
                or Path(receipt.get('clone_root', '')).resolve() != clone):
            raise ValueError('prepared clone receipt does not bind the original shard')
        if receipt.get('copied_bytes_identical') is not True or receipt.get('original_bytes_unchanged') is not True or receipt.get('new_llm_cases') != 0:
            raise ValueError('prepared clone receipt lacks unchanged byte identity or zero new model cases')
        server['clone_receipt_sha256'] = sha256_file(receipt_path)
    return value


def _geometry_signature(snapshot):
    # Hide/show is the only permitted change; style/path/identity remain exact.
    return json.dumps([{key: value for key, value in overlay.items() if key not in {'visible', 'visibility_observable'}}
                       for overlay in snapshot['overlays']], sort_keys=True, allow_nan=False)


def _image_array(data):
    return np.asarray(Image.open(io.BytesIO(data)).convert('RGB'), dtype=np.uint8)


class ChromeObserver:
    """One real context/page per worker; independent of server/Provider lifecycle."""

    def __init__(self, browser, server, output):
        self.browser, self.server, self.output = browser, server, output
        self.context = self.page = self.cdp = None
        self.forbidden_requests = []
        self.sdk_resources = {}
        self.sdk_redirects = []
        self.body_tasks = set()
        self.guard_tasks = set()
        self.background_task_errors = []
        self.projection_requests = []
        self.projection_tasks = set()
        self.projection_observations = []
        self.current_projection_reference = None
        self.current_case_output = None
        self.projection_response_sequence = 0
        self.current_case = None
        self.current_session = None
        self.storage_initialized = False
        self.cdp_scope_id = uuid.uuid4().hex
        self.page_target_id = None
        self.case_sequence = 0
        self.current_case_scope = {}
        self.request_owner_scopes = weakref.WeakKeyDictionary()
        self.received_sequence = 0
        self.paused_requests = []
        self.network_loading_failures = []
        self.network_request_starts = []
        self.network_extra_infos = []
        self.network_response_extra_infos = []
        self.native_sdk_responses = []
        self.native_sdk_body_receipts = []
        self.network_finished = []
        self.sdk_initial_200 = {}
        self.native_resource_changed = asyncio.Event()
        self.network_reconciliation_counts = {'full_calls': 0, 'event_skips': 0,
            'event_full_calls': 0, 'event_callback_errors': 0}

    def captured_scope(self):
        return {**deepcopy(self.current_case_scope), 'case_id': self.current_case,
            'cdp_scope_id': self.cdp_scope_id, 'page_target_id': self.page_target_id}

    def capture_request_owner(self, request):
        # Response arrival can be after a case switch. The actual Playwright
        # Request object remains the owner; URL matching/current UI state is not.
        if request not in self.request_owner_scopes:
            self.request_owner_scopes[request] = self.captured_scope()

    def request_owner_scope(self, request):
        scope = self.request_owner_scopes.get(request)
        if scope is None:
            raise ValueError('SDK response has no observed originating Request owner')
        return deepcopy(scope)

    def sdk_witness(self):
        return {'starts': self.network_request_starts, 'responses': self.native_sdk_responses,
            'extra_infos': self.network_extra_infos, 'bodies': self.native_sdk_body_receipts,
            'prior_receipts': list(self.sdk_initial_200.values()), 'finished': self.network_finished,
            'response_extra_infos': self.network_response_extra_infos}

    def retain_sdk_initial_200(self, observed):
        if observed.get('status') == 200 and observed.get('kind') == 'loaded':
            key = (observed['raw_url_sha256'], observed['request_id'])
            self.sdk_initial_200.setdefault(key, deepcopy(observed))

    async def native_sdk_body(self, url, status, scope, *, redirect_chain_raw_url_sha256=None):
        raw_hash = hashlib.sha256(url.encode()).hexdigest()
        if not sdk_raw_chain_shape_verified(redirect_chain_raw_url_sha256, raw_hash):
            raise ValueError('SDK actual raw chain shape is missing or unsupported')
        deadline = time.monotonic() + SDK_TIMEOUT_MS / 1000
        while time.monotonic() < deadline:
            self.native_resource_changed.clear()
            starts = [value for value in self.network_request_starts if value.get('raw_url_sha256') == raw_hash
                and all(value.get(name) == scope.get(name) for name in ('case_id', 'case_epoch', 'cdp_scope_id', 'page_target_id'))]
            if len(starts) > 1:
                raise ValueError('SDK Native request-start candidates ambiguous')
            if len(starts) == 1:
                start = starts[0]
                responses = [value for value in self.native_sdk_responses if value['request_id'] == start['request_id']]
                finished = [value for value in self.network_finished if value['request_id'] == start['request_id']]
                if len(finished) == 1 and [value.get('raw_url_sha256') for value in
                    finished[0].get('request_start_candidates', [])] != redirect_chain_raw_url_sha256:
                    raise ValueError('SDK actual raw chain does not match the Native completion source')
                extra = [value for value in self.network_extra_infos if value['request_id'] == start['request_id']]
                wire = [value for value in self.network_response_extra_infos if value['request_id'] == start['request_id']]
                two_hop = isinstance(redirect_chain_raw_url_sha256, list) and len(redirect_chain_raw_url_sha256) == 2
                maximum = 2 if two_hop else 1
                if len(responses) > 1 or len(finished) > 1 or len(wire) > maximum or len(extra) > maximum:
                    raise ValueError('SDK Native response/finish/ExtraInfo candidates ambiguous')
                wire_status = 200 if two_hop else wire[0].get('status_code') if len(wire) == 1 else None
                if wire_status == 304 and len(extra) > 1:
                    raise ValueError('SDK Native conditional request ExtraInfo ambiguous')
                if len(responses) == len(finished) == 1 and len(wire) == len(extra) == maximum:
                    if two_hop:
                        if status != 200:
                            raise ValueError('SDK cache: a redirected cached representation is not supported')
                        actual_hop = {**deepcopy(start), 'redirect_chain_raw_url_sha256': deepcopy(redirect_chain_raw_url_sha256)}
                        ordinary_two_hop_source(self.sdk_witness(), actual_hop)
                    else:
                        if not native_request_extra_verified(extra[0]):
                            raise ValueError('SDK Native actual request ExtraInfo is unknown')
                        if not native_wire_response_verified(wire[0]) or not (wire_status == 200 and responses[0]['status'] == status == 200
                            or wire_status == 304 and responses[0]['status'] in (200, 304) and status in (200, 304)):
                            raise ValueError('SDK Native wire/effective status is unknown or inconsistent')
                        if not native_completion_verified(finished[0], start) or not (type(responses[0].get('received_sequence')) is int
                            and start['received_sequence'] < responses[0]['received_sequence'] < finished[0]['received_sequence']):
                            raise ValueError('SDK Native completion source/phase does not bind exact request')
                    selected_completion = deepcopy(finished[0])
                    pre_body_authority = sdk_authority_snapshot(self.sdk_witness(), start, {start['request_id']})
                    native_scope = {**deepcopy(start), 'effective_status': responses[0]['status'],
                        'wire_status': wire_status, 'playwright_status': status,
                        'redirect_chain_raw_url_sha256': deepcopy(redirect_chain_raw_url_sha256) if redirect_chain_raw_url_sha256 is not None else [raw_hash]}
                    body_record = {**native_scope, 'operation': 'Network.getResponseBody', 'success': False,
                        'completion_proof': selected_completion}
                    try:
                        actual = await self.cdp.send('Network.getResponseBody', {'requestId': start['request_id']})
                        if type(actual.get('base64Encoded')) is not bool or not isinstance(actual.get('body'), str):
                            raise ValueError('SDK actual CDP body is unreadable')
                        data = base64.b64decode(actual['body'], validate=True) if actual['base64Encoded'] else actual['body'].encode('utf-8')
                        if not data:
                            raise ValueError('SDK actual current CDP body is empty')
                        body_record.update({'success': True, 'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data)})
                        # Awaiting the actual body can admit late Native facts.
                        post_body_authority = sdk_authority_snapshot(self.sdk_witness(), start, {start['request_id']})
                        if post_body_authority != pre_body_authority:
                            raise ValueError('SDK Native completion/authority changed or became ambiguous during body read')
                        return data, native_scope
                    except Exception as error:
                        body_record['error'] = safe_error(error)
                        raise
                    finally:
                        self.received_sequence += 1
                        body_record['received_sequence'] = self.received_sequence
                        self.native_sdk_body_receipts.append(body_record)
            try:
                await asyncio.wait_for(self.native_resource_changed.wait(), timeout=max(.001, deadline-time.monotonic()))
            except TimeoutError:
                break
        detail = {'raw_url_sha256': raw_hash, 'start_candidates': len(starts),
            'case_id': scope.get('case_id'), 'case_epoch': scope.get('case_epoch')}
        if len(starts) == 1:
            detail.update(request_id=starts[0]['request_id'], responses=len(responses),
                finished=len(finished), request_extra_infos=len(extra), response_extra_infos=len(wire))
        raise ValueError('SDK Native resource proof exceeded fixed readiness deadline: ' + json.dumps(detail))

    async def initialize(self, first_case):
        self.current_session = first_case['session_id']
        self.context = await self.browser.new_context(viewport={'width': 1600, 'height': 1040},
            device_scale_factor=1, reduced_motion='reduce', service_workers='block')
        # Runs before application JavaScript on the first origin navigation.
        # The one-time marker lets subsequent explicit LS switches survive reload.
        seed = json.dumps({'user_id': first_case['user_id'], 'session_id': first_case['session_id']})
        await self.context.add_init_script(f'''(() => {{
          if (!localStorage.getItem('gismind.__historical_observer_seeded')) {{
            const seed = {seed}; localStorage.setItem('gismind.user_id', seed.user_id);
            localStorage.setItem('gismind.active_session', seed.session_id);
            localStorage.setItem('gismind.__historical_observer_seeded', '1');
          }}
        }})();''')
        self.page = await self.context.new_page()
        self.page.set_default_timeout(SDK_TIMEOUT_MS)
        self.cdp = await self.context.new_cdp_session(self.page)
        target_info = await self.cdp.send('Target.getTargetInfo')
        self.page_target_id = target_info['targetInfo']['targetId']
        origins = [self.server['frontend_url'].rstrip('/'), self.server['backend_url'].rstrip('/')]
        self.origins = origins

        def request_start(event):
            self.received_sequence += 1
            request = event['request']
            record = {**self.captured_scope(), 'request_id': event['requestId'],
                'raw_url_sha256': hashlib.sha256(request['url'].encode()).hexdigest(), 'url': redact_url(request['url']),
                'method': request['method'], 'resource_type': event.get('type'), 'frame_id': event.get('frameId'),
                'loader_id': event.get('loaderId'), 'redirect_hop': 'redirectResponse' in event,
                'received_sequence': self.received_sequence, 'native_timestamp': event.get('timestamp'),
                'request_authority_sha256': hashlib.sha256(urlsplit(request['url']).netloc.encode()).hexdigest()}
            if 'redirectResponse' in event:
                previous = event['redirectResponse']
                headers = {name.lower(): str(value) for name, value in previous.get('headers', {}).items()}
                location = headers.get('location')
                record['redirect_source'] = {'previous_raw_url_sha256': hashlib.sha256(previous['url'].encode()).hexdigest(),
                    'status': previous.get('status'), 'location_value_sha256': hashlib.sha256(location.encode()).hexdigest() if location else None,
                    'resolved_location_raw_url_sha256': hashlib.sha256(urljoin(previous['url'], location).encode()).hexdigest() if location else None}
            record['native_identity'] = {name: record[name] for name in
                ('request_id', 'raw_url_sha256', 'method', 'frame_id', 'loader_id', 'resource_type', 'redirect_hop')}
            if 'redirect_source' in record:
                record['native_identity']['redirect_source'] = deepcopy(record['redirect_source'])
            self.network_request_starts.append(record)
            self.native_resource_changed.set()
            self.reconcile_network_event('start', record)

        def request_extra_info(event):
            self.received_sequence += 1
            self.network_extra_infos.append({'request_id': event['requestId'], 'cdp_scope_id': self.cdp_scope_id,
                'page_target_id': self.page_target_id, 'received_sequence': self.received_sequence,
                **sdk_conditional_headers(event.get('headers', {}))})
            self.native_resource_changed.set()

        def response_extra_info(event):
            self.received_sequence += 1
            record = {'request_id': event['requestId'], 'cdp_scope_id': self.cdp_scope_id,
                'page_target_id': self.page_target_id, 'received_sequence': self.received_sequence,
                'status_code': event.get('statusCode'), **sdk_conditional_headers(event.get('headers', {}))}
            record['native_identity'] = {name: record[name] for name in ('request_id', 'status_code', 'etag_sha256', 'location_value_sha256')}
            self.network_response_extra_infos.append(record)
            self.native_resource_changed.set()

        def native_response(event):
            response = event['response']
            host = (urlsplit(response['url']).hostname or '').lower()
            if event.get('type') != 'Script' or not (host == 'amap.com' or host.endswith('.amap.com') or host.endswith('.autonavi.com')):
                return
            self.received_sequence += 1
            raw_hash = hashlib.sha256(response['url'].encode()).hexdigest()
            starts = [value for value in self.network_request_starts
                if value['request_id'] == event['requestId'] and value['raw_url_sha256'] == raw_hash]
            source = starts[0] if len(starts) == 1 else {}
            self.native_sdk_responses.append({**deepcopy(source), 'request_id': event['requestId'],
                'cdp_scope_id': self.cdp_scope_id, 'page_target_id': self.page_target_id,
                'url': redact_url(response['url']), 'raw_url_sha256': raw_hash,
                'frame_id': event.get('frameId'), 'loader_id': event.get('loaderId'), 'status': response['status'],
                'resource_type': event['type'], 'received_sequence': self.received_sequence,
                'native_timestamp': event.get('timestamp'), 'sampling_scope': self.captured_scope(),
                **sdk_conditional_headers(response.get('headers', {}))})
            self.native_resource_changed.set()

        def loading_finished(event):
            self.received_sequence += 1
            self.network_finished.append(capture_native_completion(event, cdp_scope_id=self.cdp_scope_id,
                page_target_id=self.page_target_id, received_sequence=self.received_sequence,
                request_starts=self.network_request_starts))
            self.native_resource_changed.set()

        def native_handler(callback, name):
            def captured(event):
                try:
                    callback(event)
                except Exception as error:
                    self.record_callback_error(error, name)
            return captured

        for name, callback in [('Network.requestWillBeSent', request_start),
                ('Network.requestWillBeSentExtraInfo', request_extra_info), ('Network.responseReceived', native_response),
                ('Network.responseReceivedExtraInfo', response_extra_info), ('Network.loadingFinished', loading_finished)]:
            self.cdp.on(name, native_handler(callback, name))

        def loading_failed(event):
            self.received_sequence += 1
            matching = [value for value in self.paused_requests if value.get('network_id') == event.get('requestId')]
            source = matching[0] if len(matching) == 1 else {}
            observed = {**deepcopy(event), 'network_id': event.get('requestId'),
                'cdp_scope_id': self.cdp_scope_id, 'page_target_id': self.page_target_id,
                'case_id': source.get('case_id'), 'case_epoch': source.get('case_epoch'),
                'received_sequence': self.received_sequence, 'native_event': deepcopy(event),
                'cors_status_field_present': 'corsErrorStatus' in event,
                'blocked_reason_field_present': 'blockedReason' in event}
            self.network_loading_failures.append(observed)
            self.reconcile_network_event('native', observed)

        self.cdp.on('Network.loadingFailed', native_handler(loading_failed, 'Network.loadingFailed'))
        await self.cdp.send('Network.enable')

        async def paused(event, scope):
            request = event['request']
            if blocked_request(request['method'], request['url'], origins):
                self.forbidden_requests.append({'case_id': scope['case_id'], 'method': request['method'],
                    'url': redact_url(request['url']), 'blocked': True})
                await self.cdp.send('Fetch.failRequest', {'requestId': event['requestId'], 'errorReason': 'BlockedByClient'})
            else:
                parameters = {'requestId': event['requestId']}
                headers = history_projection_headers(request, origins, scope['session_id'])
                if headers is not None:
                    parameters['headers'] = headers
                    projection = {'case_id': scope['case_id'], 'session_id': scope['session_id'],
                        'method': 'GET', 'url': redact_url(request['url']), 'header_name': HISTORY_SESSION_HEADER,
                        'cdp_continue_succeeded': False}
                    self.projection_requests.append(projection)
                await self.cdp.send('Fetch.continueRequest', parameters)
                if headers is not None:
                    projection['cdp_continue_succeeded'] = True

        def pause_handler(event):
            request = event['request']
            scope = {'case_id': self.current_case, 'session_id': self.current_session}
            self.received_sequence += 1
            binding = {**deepcopy(self.current_case_scope), 'case_id': self.current_case,
                'cdp_scope_id': self.cdp_scope_id, 'page_target_id': self.page_target_id,
                'request_id': event['requestId'], 'network_id': event.get('networkId'),
                'method': request['method'], 'url': redact_url(request['url']),
                'raw_url_sha256': hashlib.sha256(request['url'].encode()).hexdigest(), 'frame_id': event.get('frameId'),
                'operation': 'Fetch.failRequest' if blocked_request(request['method'], request['url'], origins) else 'Fetch.continueRequest',
                'received_sequence': self.received_sequence}
            self.paused_requests.append(binding)
            self.reconcile_network_event('paused', binding)
            track_task(paused(event, scope), self.guard_tasks, self.background_task_errors,
                {**binding, 'task_kind': 'request_guard'}, on_error=self.reconcile_network_diagnostics)

        self.cdp.on('Fetch.requestPaused', native_handler(pause_handler, 'Fetch.requestPaused'))
        # Selective real request interception preserves authentic SDK HTTP cache;
        # never fulfills requests and never returns synthetic network responses.
        patterns = [{'urlPattern': origin + '/api/*', 'requestStage': 'Request'} for origin in origins]
        patterns.append({'urlPattern': '*://api.deepseek.com/*', 'requestStage': 'Request'})
        await self.cdp.send('Fetch.enable', {'patterns': patterns})

        async def resource(response, received_scope):
            parsed = urlsplit(response.url)
            host = (parsed.hostname or '').lower()
            if not (host == 'amap.com' or host.endswith('.amap.com') or host.endswith('.autonavi.com')):
                return
            if response.request.resource_type != 'script':
                return
            received_scope = self.request_owner_scope(response.request)
            chain = []
            request = response.request
            while request is not None:
                chain.append(request.url)
                request = request.redirected_from
            native_scope, data, witness = received_scope, None, None
            if response.status in (200, 304):
                raw_chain = [hashlib.sha256(value.encode()).hexdigest() for value in reversed(chain)]
                data, native_scope = await self.native_sdk_body(response.url, response.status, received_scope,
                    redirect_chain_raw_url_sha256=raw_chain)
                witness = self.sdk_witness() if native_scope['wire_status'] == 304 else None
            async def actual_current_body():
                return data
            observed = await observe_sdk_script_response(url=response.url, status=response.status,
                headers=response.headers, redirect_chain=list(reversed(chain)), body_reader=response.body,
                observation_scope=native_scope, cache_witness=witness) if data is None else await observe_sdk_script_response(
                    url=response.url, status=native_scope['wire_status'], headers=response.headers, redirect_chain=list(reversed(chain)),
                    body_reader=actual_current_body, observation_scope=native_scope, cache_witness=witness)
            if observed['kind'] == 'redirect':
                self.sdk_redirects.append(observed)
            else:
                self.sdk_resources[observed['raw_url_sha256']] = observed
                self.retain_sdk_initial_200(observed)

        def response_handler(response):
            received_scope = deepcopy(self.request_owner_scopes.get(response.request)) or self.captured_scope()
            parsed = urlsplit(response.url)
            origin = urlunsplit((parsed.scheme, parsed.netloc, '', '', ''))
            if response.request.method == 'GET' and parsed.path == '/api/sessions' and origin in origins:
                sequence = self.projection_response_sequence
                self.projection_response_sequence += 1
                track_task(self.capture_projection_response(response, deepcopy(self.current_projection_reference),
                    self.current_case, self.current_case_output, sequence), self.projection_tasks, self.background_task_errors,
                    {'case_id': self.current_case, 'task_kind': 'session_projection_response', 'url': redact_url(response.url)},
                    on_error=self.reconcile_network_diagnostics)
            track_task(resource(response, received_scope), self.body_tasks, self.background_task_errors,
                {**received_scope, 'task_kind': 'sdk_resource_body', 'url': redact_url(response.url)},
                on_error=self.reconcile_network_diagnostics)

        self.page.on('request', self.capture_request_owner)
        self.page.on('response', response_handler)

    async def close(self):
        await self.drain_tasks()
        if self.context:
            await self.context.close()
        await self.drain_tasks()
        self.reconcile_network_diagnostics()

    def reconcile_network_diagnostics(self):
        # Every explicit gate/drain/close remains unconditional and uncached.
        self.network_reconciliation_counts['full_calls'] += 1
        reconcile_canceled_read_errors(self.background_task_errors, self.paused_requests,
            self.network_loading_failures, self.origins, request_starts=self.network_request_starts)

    def reconcile_network_event(self, event_kind, record):
        try:
            if event_relation_requires_full(event_kind, record, self.background_task_errors):
                self.network_reconciliation_counts['event_full_calls'] += 1
                self.reconcile_network_diagnostics()
            else:
                self.network_reconciliation_counts['event_skips'] += 1
        except Exception as error:
            self.record_callback_error(error, 'network_relation:' + str(event_kind))

    def record_callback_error(self, error, name):
        def message(value):
            try:
                return safe_error(value)
            except Exception:
                return type(value).__name__ + ': exception formatting unavailable'
        self.network_reconciliation_counts['event_callback_errors'] += 1
        self.background_task_errors.append({**self.captured_scope(), 'task_kind': 'native_event_observer',
            'classification': 'observer_native_event_exception', 'native_event_name': name,
            'gate_failure': True, 'error': message(error)})
        try:
            self.reconcile_network_diagnostics()
        except Exception as replay_error:
            self.background_task_errors.append({**self.captured_scope(), 'task_kind': 'native_event_observer',
                'classification': 'observer_reconcile_callback_exception', 'native_event_name': name,
                'gate_failure': True, 'error': message(replay_error)})

    async def drain_tasks(self):
        while self.guard_tasks or self.body_tasks or self.projection_tasks:
            pending = tuple(self.guard_tasks | self.body_tasks | self.projection_tasks)
            try:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=SDK_TIMEOUT_MS / 1000)
            except asyncio.TimeoutError:
                # wait_for cancellation is journaled by each completion callback.
                self.background_task_errors.append({'case_id': self.current_case, 'task_kind': 'task_drain',
                    'classification': 'bounded_task_drain_timeout', 'request_cancellation_confirmed': False,
                    'gate_failure': True, 'error': 'background tasks exceeded fixed drain deadline'})
                self.reconcile_network_diagnostics()
        self.reconcile_network_diagnostics()

    async def get_clone_bytes(self, path, user_id):
        response = await self.context.request.get(self.server['backend_url'].rstrip('/') + path,
            headers={'X-User-Id': user_id}, timeout=SDK_TIMEOUT_MS)
        if response.status != 200:
            raise ValueError(f'clone readonly GET failed with HTTP {response.status}')
        return await response.body()

    async def prepare_projection_reference(self, case, case_output):
        backend_origin = self.server['backend_url'].rstrip('/')
        frontend_origin = self.server['frontend_url'].rstrip('/')
        headers = {'X-User-Id': case['user_id']}
        # These independent reference reads deliberately send no selector. They
        # do not use browser interception or the projected collection response.
        recent_response = await self.context.request.get(backend_origin + '/api/sessions', headers=headers, timeout=SDK_TIMEOUT_MS)
        recent_body = await recent_response.body()
        selected_response = await self.context.request.get(backend_origin + '/api/sessions/' + case['session_id'], headers=headers, timeout=SDK_TIMEOUT_MS)
        selected_body = await selected_response.body()
        (case_output / 'projection-reference-stock-recent.json').write_bytes(recent_body)
        (case_output / 'projection-reference-selected-metadata.json').write_bytes(selected_body)
        reference = make_projection_reference(selector=case['session_id'], frontend_origin=frontend_origin,
            backend_origin=backend_origin, stock_url=recent_response.url, stock_status=recent_response.status,
            stock_body=recent_body, selected_url=selected_response.url, selected_status=selected_response.status,
            selected_body=selected_body)
        reference.update({'case_id': case['case_id'], 'clone_receipt_sha256': self.server.get('clone_receipt_sha256'),
            'reference_gets_sent_selector_header': False})
        write_json(case_output / 'projection-reference.json', reference)
        self.current_projection_reference = reference
        return reference

    async def capture_projection_response(self, response, reference, case_id, case_output, sequence):
        if reference is None or case_output is None:
            raise ValueError('projection actual browser response has no current original reference binding')
        body = await response.body()
        safe_headers = {name: value for name, value in response.headers.items()
            if name.lower() in {HISTORY_SESSION_HEADER.lower(), HISTORY_POLICY_HEADER.lower(), 'content-type'}}
        raw_path = case_output / f'projection-browser-response-{sequence:04d}.json'
        raw_path.write_bytes(body)
        observation = {'case_id': case_id, 'selector_session_id': reference['selector_session_id'], 'passed': False,
            'response_url': redact_url(response.url), 'response_status': response.status,
            'response_headers': safe_headers, 'response_body_sha256': hashlib.sha256(body).hexdigest(),
            'response_body_path': str(raw_path.relative_to(self.output))}
        try:
            observation.update(verify_session_projection(reference, url=response.url, status=response.status,
                headers=safe_headers, body=body))
        except Exception as error:
            observation['error'] = safe_error(error)
            raise
        finally:
            self.projection_observations.append(observation)
            write_json(case_output / f'projection-browser-receipt-{sequence:04d}.json', observation)

    async def require_projection_response(self, case):
        if self.projection_tasks:
            await asyncio.wait_for(asyncio.gather(*tuple(self.projection_tasks), return_exceptions=True), timeout=SDK_TIMEOUT_MS / 1000)
        observations = [value for value in self.projection_observations if value.get('case_id') == case['case_id']]
        if not projection_observations_verified(observations, case['session_id']):
            raise ValueError('projection actual browser response provenance was not independently verified')

    async def recover(self, case):
        await self.page.bring_to_front()
        if not self.storage_initialized:
            await self.page.goto(self.server['frontend_url'], wait_until='domcontentloaded')
            self.storage_initialized = True
        else:
            await self.page.evaluate('''seed => {
              localStorage.setItem('gismind.user_id',seed.user_id);
              localStorage.setItem('gismind.active_session',seed.session_id);
            }''', {'user_id': case['user_id'], 'session_id': case['session_id']})
            await self.page.reload(wait_until='domcontentloaded')
        workspace = self.page.get_by_label('成果工作区', exact=True)
        await workspace.wait_for(state='visible')
        await self.page.wait_for_function('''runId => {
          const nodes = [...document.querySelectorAll('[aria-label="成果工作区"]')];
          return nodes.length === 1 && nodes[0].getAttribute('data-result-run-id') === runId;
        }''', arg=case['run_id'], timeout=SDK_TIMEOUT_MS)
        await self.require_projection_response(case)
        await workspace.locator(f'button[data-artifact-id="{case["map_artifact"]["artifact_id"]}"]').click()
        await self.page.wait_for_function('''artifactId => [...document.querySelectorAll('[data-map-render-evidence]')].some(node => {
          try { const evidence=JSON.parse(node.getAttribute('data-map-render-evidence')); return node.isConnected && evidence.sourceArtifactId===artifactId; }
          catch { return false; }
        })''', arg=case['map_artifact']['artifact_id'], timeout=SDK_TIMEOUT_MS)
        prototype = await self.cdp.send('Runtime.evaluate', {'expression': 'window.AMap.Map.prototype', 'returnByValue': False})
        prototype_id = prototype.get('result', {}).get('objectId')
        if not prototype_id:
            raise ValueError('actual loaded AMap.Map prototype unavailable')
        instances = await self.cdp.send('Runtime.queryObjects', {'prototypeObjectId': prototype_id})
        object_id = instances['objects']['objectId']
        attached = await self.cdp.send('Runtime.callFunctionOn', {'objectId': object_id,
            'functionDeclaration': SDK_ATTACH_JS, 'arguments': [{'value': case['map_artifact']['artifact_id']}], 'returnByValue': True})
        if attached.get('exceptionDetails'):
            raise ValueError('actual SDK source-bound map discovery failed: ' + json.dumps(attached['exceptionDetails'].get('text')))
        await self.cdp.send('Runtime.releaseObject', {'objectId': object_id})
        await self.cdp.send('Runtime.releaseObject', {'objectId': prototype_id})
        return workspace

    async def snapshot(self):
        raw = await self.page.evaluate(SDK_SNAPSHOT_JS)
        raw['overlays'] = [normalize_sdk_overlay(overlay) for overlay in raw['overlays']]
        for overlay in raw['overlays']:
            validate_overlay_style(overlay)
        return raw

    async def settled_initial_snapshot(self):
        deadline = time.monotonic() + SDK_TIMEOUT_MS / 1000
        previous = None
        stable = 0
        while time.monotonic() < deadline:
            current = await self.snapshot()
            signature = view_signature(current) + _geometry_signature(current)
            stable = stable + 1 if signature == previous else 0
            if stable >= 3:
                return current
            previous = signature
            await self.page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        raise ValueError('bounded SDK readiness: actual initial camera/layout did not stabilize')

    async def frame(self, signature, geometry_signature, deadline, *, index, shown, diagnostic_output=None):
        started = time.monotonic()
        diagnostic = {'passed': False, 'overlay_index': index, 'shown': shown,
            'started_monotonic': started, 'deadline_monotonic': deadline, 'deadline_extended': False,
            'attempts': 0, 'screenshot_return_count': 0, 'last_global_drift': None,
            'stable_consecutive_comparisons': 0, 'operations': [], 'operations_dropped': 0,
            'operation_totals': {}, 'raw_png_samples': []}
        samples = []

        def remaining():
            return deadline - time.monotonic()

        def require_budget(stage):
            require_paint_deadline(deadline, stage)

        def retain_png(data):
            diagnostic['screenshot_return_count'] += 1
            samples.append({'bytes': data, 'sample_index': diagnostic['screenshot_return_count'],
                'captured_monotonic': time.monotonic(), 'camera_geometry_post_capture_verified': False})
            del samples[:-3]

        async def operation(stage, action, accept=None):
            require_budget(stage + ':before')
            before = time.monotonic()
            item = {'stage': stage, 'started_monotonic': before, 'remaining_before_seconds': deadline-before}
            try:
                # A fresh remaining timeout bounds every await, not a fresh deadline.
                result = await asyncio.wait_for(action(), timeout=deadline-before)
                if accept:
                    accept(result)
                item['status'] = 'returned'
            except Exception as error:
                item.update({'status': 'error', 'error': safe_error(error)})
                if isinstance(error, (TimeoutError, asyncio.TimeoutError)) or type(error).__name__ == 'TimeoutError':
                    kind = ('font_capture_timeout' if stage == 'screenshot' and 'fonts' in str(error).lower()
                        else 'screenshot_capture_timeout' if stage == 'screenshot' else 'paint_budget_exhausted')
                    message = ('fixed paint budget exhausted during ' if kind == 'paint_budget_exhausted'
                        else 'fixed paint capture timeout at ') + stage + ': ' + safe_error(error)
                    raise PaintCaptureError(message, failure_kind=kind) from error
                raise
            finally:
                item.update({'elapsed_seconds': max(0, time.monotonic()-before),
                    'remaining_after_seconds': max(0, remaining())})
                diagnostic['operations'].append(item)
                if len(diagnostic['operations']) > 128:
                    diagnostic['operations'].pop(0)
                    diagnostic['operations_dropped'] += 1
                total = diagnostic['operation_totals'].setdefault(stage, {'count': 0, 'elapsed_seconds': 0.0})
                total['count'] += 1
                total['elapsed_seconds'] += item['elapsed_seconds']
            require_budget(stage + ':after')
            return result

        def persist(*, failed):
            diagnostic.update({'elapsed_seconds': max(0, time.monotonic()-started),
                'deadline_remaining_seconds': max(0, remaining()),
                'observed_pixels_unstable': diagnostic['last_global_drift'] is not None and diagnostic['last_global_drift'] > .002})
            if diagnostic_output is not None:
                target = Path(diagnostic_output)
                target.mkdir(parents=True, exist_ok=True)
                if failed:
                    for sample in samples:
                        path = target / f"sample-{sample['sample_index']:04d}.png"
                        path.write_bytes(sample['bytes'])
                        diagnostic['raw_png_samples'].append({key: value for key, value in sample.items() if key != 'bytes'} |
                            {'capture_path': str(path.relative_to(self.output)), 'capture_sha256': hashlib.sha256(sample['bytes']).hexdigest(),
                             'size_bytes': len(sample['bytes'])})
                write_json(target / 'diagnostic.json', diagnostic)

        try:
            require_budget('frame-entry')
            await operation('visibility', lambda: self.page.evaluate(SDK_VISIBILITY_JS, {'index': index, 'shown': shown}))
            previous = None
            while remaining() > 0:
                await operation('animation_frames', lambda: self.page.evaluate(
                    '() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))'))
                before = await operation('before_snapshot', self.snapshot)
                require_fixed_view(signature, before)
                if _geometry_signature(before) != geometry_signature:
                    raise ValueError('actual SDK geometry/style changed during hide/show')
                for overlay in before['overlays']:
                    expected_visible = shown and (index is None or overlay['overlay_index'] == index)
                    if overlay['visibility_observable'] and overlay['visible'] != expected_visible:
                        break
                else:
                    require_budget('screenshot-dispatch')
                    data = await operation('screenshot', lambda: self.page.screenshot(clip=before['view']['rect'],
                        scale='css', timeout=remaining()*1000), retain_png)
                    after = await operation('after_snapshot', self.snapshot)
                    require_fixed_view(signature, after)
                    if _geometry_signature(after) != geometry_signature:
                        raise ValueError('actual SDK geometry/style changed across screenshot')
                    samples[-1]['camera_geometry_post_capture_verified'] = True
                    pixels = _image_array(data)
                    diagnostic['attempts'] += 1
                    if previous is not None:
                        drift = float(np.mean(np.max(np.abs(pixels.astype(np.int16)-previous.astype(np.int16)), axis=2) >= 3))
                        diagnostic['last_global_drift'] = drift
                        diagnostic['stable_consecutive_comparisons'] = diagnostic['stable_consecutive_comparisons']+1 if drift <= .002 else 0
                        if diagnostic['stable_consecutive_comparisons'] >= 2:
                            require_budget('stable-frame-return')
                            diagnostic['passed'] = True
                            persist(failed=False)
                            require_budget('diagnostic-persistence:after')
                            return {'bytes': data, 'pixels': pixels, 'snapshot': after,
                                'stable_consecutive_comparisons': diagnostic['stable_consecutive_comparisons'],
                                'attempts': diagnostic['attempts'], 'last_global_drift': drift,
                                'capture_diagnostics': diagnostic}
                    previous = pixels
            raise PaintCaptureError('fixed paint budget exhausted; actual pixel stability was not proved')
        except Exception as error:
            diagnostic.update({'passed': False, 'failure_kind': getattr(error, 'failure_kind', 'capture_invariant_error'),
                'failure_stage': getattr(error, 'stage', diagnostic['operations'][-1]['stage'] if diagnostic['operations'] else 'frame-entry'),
                'original_error': safe_error(error)})
            persist(failed=True)
            error.diagnostics = diagnostic
            raise

    async def capture_phase_sequence(self, signature, geometry_signature, deadline, *, index,
            capture_output, prefix='', sequence_kind='overlay'):
        target = Path(capture_output)
        target.mkdir(parents=True, exist_ok=True)
        frames = []
        journal = {'sequence_kind': sequence_kind, 'paint_readiness': 'incomplete', 'frames': [],
            'deadline_monotonic': deadline, 'deadline_extended': False}
        phase = None
        try:
            for phase, shown in [('hidden-before', False), ('shown-first', True), ('hidden-after', False), ('shown-again', True)]:
                require_paint_deadline(deadline, phase + ':before-frame-await')
                frame = await self.frame(signature, geometry_signature, deadline, index=index, shown=shown,
                    diagnostic_output=target / (prefix + phase + '-diagnostics'))
                require_paint_deadline(deadline, phase + ':after-frame-await')
                persistence_started = time.monotonic()
                path = target / (prefix + phase + '.png')
                path.write_bytes(frame['bytes'])
                frame.update({'phase': phase, 'capture_path': str(path.relative_to(self.output)),
                    'capture_sha256': hashlib.sha256(frame['bytes']).hexdigest()})
                frames.append(frame)
                frame['png_persistence_elapsed_seconds'] = time.monotonic()-persistence_started
                journal['frames'].append({key: value for key, value in frame.items() if key not in {'bytes', 'pixels'}})
                write_json(target / 'sequence.json', journal)
                require_paint_deadline(deadline, phase + ':after-evidence-persistence')
            journal['paint_readiness'] = 'pending_oracle'
            write_json(target / 'sequence.json', journal)
            require_paint_deadline(deadline, 'sequence-return')
            return frames
        except Exception as error:
            journal.update({'paint_readiness': 'incomplete', 'failed_phase': phase, 'error': safe_error(error),
                'deadline_remaining_seconds': max(0, deadline-time.monotonic()),
                'failed_frame_diagnostics': getattr(error, 'diagnostics', None)})
            write_json(target / 'sequence.json', journal)
            error.capture_sequence_metadata = journal
            raise

    async def cleanup_overlays(self, record, geometry_signature):
        # Cleanup has a separate fixed bound. It never extends paint or adds
        # screenshots/oracle passes to repair missing original causal phases.
        prior_restored = record.get('restored_all_shown') is True
        record['restored_all_shown'] = False
        record['cleanup_observer_refs_released'] = False
        started = time.monotonic()
        deadline = started + SDK_TIMEOUT_MS / 1000
        diagnostic = {'started_monotonic': started, 'deadline_monotonic': deadline,
            'deadline_extended': False, 'operations': [], 'passed': False}
        failed = False
        proof_complete = False

        async def operation(stage, action):
            diagnostic['current_stage'] = stage
            before = time.monotonic()
            if before >= deadline:
                raise TimeoutError('fixed cleanup deadline exhausted before ' + stage)
            item = {'stage': stage, 'remaining_before_seconds': deadline-before}
            try:
                result = await asyncio.wait_for(action(), timeout=deadline-before)
                item['status'] = 'returned'
            except Exception as error:
                item.update({'status': 'error', 'error': safe_error(error)})
                raise
            finally:
                item.update({'elapsed_seconds': max(0, time.monotonic()-before),
                    'remaining_after_seconds': max(0, deadline-time.monotonic())})
                diagnostic['operations'].append(item)
            if time.monotonic() >= deadline:
                raise TimeoutError('fixed cleanup deadline exhausted after ' + stage)
            return result

        def failure(error):
            nonlocal failed
            failed = True
            diagnostic.update({'failed_stage': diagnostic['current_stage'], 'error': safe_error(error)})
            record['cleanup_error'] = safe_error(error)
            record['passed'] = False

        try:
            await operation('visibility', lambda: self.page.evaluate(SDK_VISIBILITY_JS, {'index': None, 'shown': True}))
            snapshot = await operation('snapshot', self.snapshot)
            record['cleanup_sdk_snapshot'] = snapshot
            record['cleanup_geometry_oracle'] = verify_sdk_geometry(record['independent_expected_gcj02_features'], snapshot['overlays'])
            if any(value['visibility_observable'] and value['visible'] is not True for value in snapshot['overlays']):
                raise ValueError('cleanup actual observable overlay is not shown')
            if geometry_signature is not None and _geometry_signature(snapshot) != geometry_signature:
                raise ValueError('final cleanup changed actual geometry/style')
            phases = record.get('final_all_phases', [])
            oracles = record.get('final_all_shown_causal_paint_oracles', [])
            proof_complete = (prior_restored and record.get('final_geometry_oracle', {}).get('passed') is True
                and len(oracles) == len(record['independent_expected_gcj02_features']) and bool(oracles)
                and all(value.get('passed') is True for value in oracles)
                and [value.get('phase') for value in phases] == ['hidden-before', 'shown-first', 'hidden-after', 'shown-again'])
            if proof_complete:
                final = record['final_sdk_snapshot']
                require_fixed_view(view_signature(final), snapshot)
                if _geometry_signature(final) != _geometry_signature(snapshot):
                    raise ValueError('cleanup differs from independently proved final shown geometry/style')
        except Exception as error:
            failure(error)
        finally:
            if time.monotonic() < deadline:
                try:
                    released = await operation('release_refs', lambda: self.page.evaluate(
                        '() => { delete window.__gismindHistoricalObserver; return !("__gismindHistoricalObserver" in window); }'))
                    if released is not True:
                        raise ValueError('observer SDK instrumentation reference release was not confirmed')
                    record['cleanup_observer_refs_released'] = True
                except Exception as error:
                    failure(error)
            record['restored_all_shown'] = proof_complete and not failed and record['cleanup_observer_refs_released']
            if not record['restored_all_shown']:
                record['passed'] = False
                record['cleanup_restoration_unproved_reason'] = 'cleanup cannot replace absent/failed final causal paint proof or failed SDK restoration'
            diagnostic.update({'passed': not failed, 'restored_all_shown': record['restored_all_shown'],
                'elapsed_seconds': max(0, time.monotonic()-started), 'deadline_remaining_seconds': max(0, deadline-time.monotonic())})
            record['cleanup_diagnostics'] = diagnostic

    async def audit_case(self, case):
        self.current_case = case['case_id']
        self.current_session = case['session_id']
        self.case_sequence += 1
        self.current_case_scope = {'case_epoch': f'{self.cdp_scope_id}:{self.case_sequence}',
            'session_id': case['session_id'], 'run_id': case['run_id'],
            'artifact_ids': [value['artifact_id'] for value in case['original_output_artifacts']]}
        forbidden_start = len(self.forbidden_requests)
        case_output = self.output / case['shard'] / case['case_id']
        case_output.mkdir(parents=True, exist_ok=False)
        self.current_case_output = case_output
        self.current_projection_reference = None
        record = {'observer_version': OBSERVER_VERSION, 'case_id': case['case_id'], 'run_id': case['run_id'],
            'session_id': case['session_id'], 'shard': case['shard'], 'map_artifact_id': case['map_artifact']['artifact_id'],
            'map_content_sha256': case['map_artifact']['content_hash'], 'gpkg_artifact_id': case['file_artifact']['artifact_id'],
            'gpkg_sha256': case['file_artifact']['content_hash'], 'original_result_sha256': case['result_sha256'],
            'original_trace_sha256': case['trace_sha256'], 'passed': False, 'forbidden_requests': [], 'overlay_observations': [],
            'background_task_errors': [], 'restored_all_shown': False, 'historical_session_readonly_projection_enabled': True,
            'historical_session_readonly_projection_verified': False,
            'stock_session_list_recent_limit': 200}
        attached = False
        geometry_signature = None
        try:
            record['historical_session_projection_reference'] = await self.prepare_projection_reference(case, case_output)
            snapshot = json.loads(await self.get_clone_bytes('/api/runs/' + case['run_id'], case['user_id']))
            verify_clone_snapshot(case, snapshot)
            for artifact in (case['map_artifact'], case['file_artifact']):
                content = await self.get_clone_bytes('/api/artifacts/' + artifact['artifact_id'] + '/content', case['user_id'])
                if hashlib.sha256(content).hexdigest() != artifact['content_hash']:
                    raise ValueError('clone content response differs from original local downloaded bytes')
            expected = independent_expected_features(case['gpkg_path'])
            record['independent_expected_gcj02_features'] = expected
            await self.recover(case)
            attached = True
            initial = await self.settled_initial_snapshot()
            if any(overlay['visibility_observable'] and overlay['visible'] is False for overlay in initial['overlays']):
                raise ValueError('initial actual SDK overlay is hidden; original all-shown state is unproved')
            signature, geometry_signature = view_signature(initial), _geometry_signature(initial)
            initial_oracle = verify_sdk_geometry(expected, initial['overlays'])
            record['initial_sdk_snapshot'] = initial
            record['initial_geometry_oracle'] = initial_oracle
            projected = await self.page.evaluate(SDK_PROJECT_JS, expected)
            record['independently_projected_expected_pixels'] = projected
            deadline = time.monotonic() + PAINT_TIMEOUT_SECONDS
            record['paint_work_budget'] = {'timeout_seconds': PAINT_TIMEOUT_SECONDS, 'deadline_monotonic': deadline,
                'deadline_extended': False, 'target_overlay_count': len(expected),
                'minimum_frame_phases': 1 + 4*len(expected) + 4, 'minimum_screenshot_count': (1 + 4*len(expected) + 4)*3,
                'includes': 'initial, all independent target phases, final phases, synchronous mask/oracle/evidence work'}
            initial_frame = await self.frame(signature, geometry_signature, deadline, index=None, shown=True,
                diagnostic_output=case_output / 'initial-frame-diagnostics')
            (case_output / 'initial-before-paint-readiness.png').write_bytes(initial_frame['bytes'])
            record['initial_capture_paint_readiness'] = 'not_yet_proved'
            shape = initial_frame['pixels'].shape[:2]
            masks = [build_masks(value['kind'], value['coordinates'], shape) for value in projected]
            for pair in initial_oracle['pairs']:
                expected_index, overlay_index = pair['expected_index'], pair['observed_index']
                overlay_output = case_output / f'overlay-{overlay_index:02d}'
                overlay_output.mkdir()
                mask = masks[expected_index]
                for name in ('core_mask', 'contour_mask', 'allowed_mask'):
                    if mask[name] is not None:
                        Image.fromarray(mask[name].astype(np.uint8) * 255).save(overlay_output / (name + '.png'))
                measurements = []
                observation = {'expected_index': expected_index, 'overlay_index': overlay_index, 'passed': False,
                    'independent_geometry': expected[expected_index], 'expected_projected_pixels': projected[expected_index],
                    'mask_recipe': mask['recipe'], 'measurements': measurements}

                async def measure(index):
                    measurement_output = overlay_output / f'measurement-{index:03d}'
                    measurement_output.mkdir()
                    try:
                        frames = await self.capture_phase_sequence(signature, geometry_signature, deadline,
                            index=overlay_index, capture_output=measurement_output)
                    except Exception as error:
                        write_json(measurement_output / 'measurement.json', {'paint_readiness': 'incomplete',
                            **getattr(error, 'capture_sequence_metadata', {}),
                            'error': safe_error(error)})
                        raise
                    metadata = {'frames': [{key: value for key, value in frame.items() if key not in {'bytes', 'pixels'}} for frame in frames]}
                    # A pending four-phase attempt is durable even if a later
                    # browser protocol operation fails before its oracle result.
                    write_json(measurement_output / 'measurement.json', {**metadata, 'paint_readiness': 'pending_oracle'})
                    return [frame['pixels'] for frame in frames], metadata

                try:
                    statistics = await wait_for_paint_ready(measure, deadline=deadline, attempts=measurements,
                        core_mask=mask['core_mask'], contour_mask=mask['contour_mask'], allowed_mask=mask['allowed_mask'])
                    observation.update({'passed': True, 'paint_readiness': 'ready', 'statistics': statistics})
                except Exception as error:
                    observation.update({'paint_readiness': 'not_ready', 'error': safe_error(error)})
                    write_json(overlay_output / 'observation.json', observation)
                    record['overlay_observations'].append(observation)
                    raise
                for index, measurement in enumerate(measurements):
                    write_json(overlay_output / f'measurement-{index:03d}' / 'measurement.json', measurement)
                write_json(overlay_output / 'observation.json', observation)
                record['overlay_observations'].append(observation)
            # Final causal captures prove every independent feature remains
            # visible in the restored all-shown composition. Other verified
            # features are allowed only inside their independently bounded union.
            final_frames = await self.capture_phase_sequence(signature, geometry_signature, deadline,
                index=None, capture_output=case_output, prefix='final-all-', sequence_kind='final')
            union_allowed = np.logical_or.reduce([value['allowed_mask'] for value in masks])
            require_paint_deadline(deadline, 'final-locked-oracles:before')
            final_oracles_started = time.monotonic()
            final_paint = [verify_visible_paint(*(frame['pixels'] for frame in final_frames),
                core_mask=mask['core_mask'], contour_mask=mask['contour_mask'], allowed_mask=union_allowed) for mask in masks]
            record['final_paint_oracle_elapsed_seconds'] = time.monotonic()-final_oracles_started
            require_paint_deadline(deadline, 'final-locked-oracles:after')
            final_frame = final_frames[-1]
            final = final_frame['snapshot']
            final_oracle = verify_sdk_geometry(expected, final['overlays'])
            (case_output / 'final-all-shown.png').write_bytes(final_frame['bytes'])
            final_drift = float(np.mean(np.max(np.abs(final_frames[1]['pixels'].astype(np.int16)-final_frame['pixels'].astype(np.int16)), axis=2) >= 3))
            if final_drift > .002:
                raise ValueError('restored all-shown pixels differ between confirmed all-shown captures')
            final_local_drift = float(np.mean(np.max(np.abs(final_frames[1]['pixels'].astype(np.int16)-final_frame['pixels'].astype(np.int16)), axis=2)[union_allowed] >= 3))
            if final_local_drift > .002:
                raise ValueError('restored local geometry pixels differ between confirmed all-shown captures')
            require_paint_deadline(deadline, 'complete-paint-proof:after')
            record.update({'final_sdk_snapshot': final, 'final_geometry_oracle': final_oracle,
                'final_all_shown_causal_paint_oracles': final_paint,
                'final_all_phases': [{key: value for key, value in frame.items() if key not in {'bytes', 'pixels'}} for frame in final_frames],
                'restored_all_shown': True, 'final_all_shown_global_drift': final_drift, 'final_all_shown_local_drift': final_local_drift})
            if self.body_tasks:
                await asyncio.gather(*self.body_tasks, return_exceptions=True)
            record.update(sdk_receipts_for_case(self.sdk_resources.values(), self.sdk_redirects,
                {**self.current_case_scope, 'case_id': case['case_id'],
                 'cdp_scope_id': self.cdp_scope_id, 'page_target_id': self.page_target_id}))
            record['sdk_resource_consumer_scope'] = {**deepcopy(self.current_case_scope), 'case_id': case['case_id'],
                'cdp_scope_id': self.cdp_scope_id, 'page_target_id': self.page_target_id}
            sdk_verification = verify_sdk_resources_indexed(record['sdk_resources'], self.sdk_witness())
            sdk_identity = (bool(record['sdk_resources'])
                and all(sdk_verification['results']) and sdk_consumer_binding_verified(record)
                and any(urlsplit(value['url']).hostname == 'webapi.amap.com' for value in record['sdk_resources']))
            record['sdk_verification_pass'] = {key: sdk_verification[key] for key in ('predicate_calls', 'indexed', 'witness_drift')}
            record['sdk_resource_native_identity_verified'] = sdk_identity
            record['forbidden_requests'] = self.forbidden_requests[forbidden_start:]
            self.reconcile_network_diagnostics()
            record['background_task_errors'] = [value for value in self.background_task_errors if value.get('case_id') in (None, case['case_id'])]
            record['historical_session_projection_responses'] = [value for value in self.projection_observations if value.get('case_id') == case['case_id']]
            record['historical_session_readonly_projection_verified'] = projection_observations_verified(
                record['historical_session_projection_responses'], case['session_id'])
            record['passed'] = observation_passed(geometry=final_oracle['passed'],
                pixels=len(record['overlay_observations']) == len(expected), restored=record['restored_all_shown'],
                sdk_identity=sdk_identity, forbidden_requests=record['forbidden_requests'],
                background_task_errors=record['background_task_errors'],
                session_projection_verified=record['historical_session_readonly_projection_verified'])
            if not record['passed']:
                raise ValueError('complete geometry/paint/restoration/SDK/readonly conjunction was not proved')
        except Exception as error:
            record['error'] = safe_error(error)
            record['passed'] = False
            if hasattr(error, 'diagnostics'):
                record['paint_capture_failure'] = error.diagnostics
            if hasattr(error, 'capture_sequence_metadata'):
                record['incomplete_capture_sequence'] = error.capture_sequence_metadata
                if error.capture_sequence_metadata['sequence_kind'] == 'final':
                    record['final_all_phases'] = error.capture_sequence_metadata['frames']
        finally:
            if attached:
                await self.cleanup_overlays(record, geometry_signature)
            record['forbidden_requests'] = self.forbidden_requests[forbidden_start:]
            self.reconcile_network_diagnostics()
            record['background_task_errors'] = [value for value in self.background_task_errors if value.get('case_id') in (None, case['case_id'])]
            record['historical_session_projection_requests'] = [value for value in self.projection_requests if value.get('case_id') == case['case_id']]
            record['historical_session_projection_responses'] = [value for value in self.projection_observations if value.get('case_id') == case['case_id']]
            record['historical_session_readonly_projection_verified'] = projection_observations_verified(
                record['historical_session_projection_responses'], case['session_id'])
            record['network_cancellation_evidence'] = [value for value in self.network_loading_failures if value.get('case_id') == case['case_id']]
            record['fetch_request_paused_evidence'] = [value for value in self.paused_requests if value.get('case_id') == case['case_id']]
            if record['forbidden_requests'] or gate_task_errors(record['background_task_errors']) or not record['historical_session_readonly_projection_verified']:
                record['passed'] = False
            record['network_reconciliation_counts'] = deepcopy(self.network_reconciliation_counts)
            authority = getattr(self, 'receipt_authority', None)
            write_json(case_output / 'observation.json', authority.compact_record(record) if authority is not None else record)
        return record


async def run_observer(args):
    from playwright.async_api import async_playwright
    original = args.original.resolve()
    output = validate_output(original, args.output)
    seal_before = verify_evidence_seal(original)
    if seal_before['verified_file_count'] != EXPECTED_SEALED_FILES:
        raise ValueError('original complete evidence seal does not contain the independently reviewed 21515 files')
    cases, excluded = load_original(original)
    servers = validate_server_map(original, read_json(args.server_map))
    frozen_source = read_json(original / 'source-manifest.json')
    source_before = source_hashes()
    if source_before != frozen_source:
        raise ValueError('frozen original application/browser source differs before observer; audit refused')
    named_original = {name: sha256_file(original / name) for name in (
        'source-manifest.json', 'corpus.json', 'summary.json', 'results.jsonl', 'schedule.json', 'evidence-manifest.json')}
    selected = select_original_cases(cases, limit=args.limit, case_ids_file=args.case_ids_file)
    selection = {'kind': 'named_context_retest' if args.case_ids_file else 'prefix_pilot' if args.limit else 'full',
        'case_ids': [case['case_id'] for case in selected] if args.case_ids_file else None}
    observer_files = {path.name: sha256_file(path) for path in (Path(__file__), TOOLING_ROOT / 'test_audit_rendered_results.py',
        TOOLING_ROOT / 'sdk_geometry_oracle.py', TOOLING_ROOT / 'visible_paint_oracle.py', TOOLING_ROOT / 'history_readonly_server.py')}
    output.mkdir(exist_ok=False)
    receipt_authority = SdkReceiptAuthority(output)
    failure_budget = CooperativeFailureBudget(args.max_failures)
    write_json(output / 'audit-start.json', {'observer_version': OBSERVER_VERSION, 'original': str(original),
        'original_named_file_sha256': named_original, 'observer_files_sha256': observer_files,
        'source_before': source_before, 'excluded_rasters': excluded, 'selected_map_count': len(selected),
        'selection': selection,
        'original_evidence_seal_before': seal_before, 'historical_session_readonly_projection_enabled': True,
        'stock_session_list_recent_limit': 200,
        'server_map': servers, 'limit': args.limit, 'provider_requests_authorized': 0, 'network_fulfillment': False,
        'paint_timeout_seconds': PAINT_TIMEOUT_SECONDS, 'browser_case_concurrency': 1,
        'shard_context_workers': args.concurrency, 'max_cases_per_context': MAX_CASES_PER_CONTEXT,
        'cooperative_fail_fast': failure_budget.report(selected),
        'evidence_schema': COMPACT_EVIDENCE_SCHEMA,
        'evidence_storage': 'complete SDK receipt payload sidecars + ordered owner/usage refs; complete per-case files + bound thin summary indexes'})
    assignments = assign_workers(selected, args.concurrency)
    records, worker_errors, all_forbidden, all_sdk, all_task_errors, all_projection_requests, all_projection_observations, all_sdk_redirects, all_loading_failures, all_paused_requests = [], [], [], [], [], [], [], [], [], []
    all_starts, all_extra_infos, all_sdk_responses, all_sdk_bodies, all_initial_200, all_finished, all_response_extra_infos = [], [], [], [], [], [], []
    all_reconciliation_statistics = []
    browser_case_lock = asyncio.Lock()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel='chrome', headless=True)
        chrome_version = browser.version

        async def worker(number):
            observer = None
            async def audit_one(case):
                nonlocal observer
                # Keep context/page per shard per worker so HTTP cache remains
                # authentic and LS switches affect only the intended origin.
                if (observer is None or observer.server is not servers[case['shard']]
                        or observer.case_sequence >= MAX_CASES_PER_CONTEXT):
                    if observer:
                        await observer.close()
                        all_forbidden.extend(observer.forbidden_requests); all_sdk.extend(observer.sdk_resources.values())
                        all_task_errors.extend(observer.background_task_errors); all_projection_requests.extend(observer.projection_requests)
                        all_projection_observations.extend(observer.projection_observations)
                        all_sdk_redirects.extend(observer.sdk_redirects)
                        all_loading_failures.extend(observer.network_loading_failures); all_paused_requests.extend(observer.paused_requests)
                        all_starts.extend(observer.network_request_starts); all_extra_infos.extend(observer.network_extra_infos)
                        all_sdk_responses.extend(observer.native_sdk_responses); all_sdk_bodies.extend(observer.native_sdk_body_receipts)
                        all_initial_200.extend(observer.sdk_initial_200.values())
                        all_finished.extend(observer.network_finished); all_response_extra_infos.extend(observer.network_response_extra_infos)
                        all_reconciliation_statistics.append({'cdp_scope_id': observer.cdp_scope_id,
                            'page_target_id': observer.page_target_id, 'counts': deepcopy(observer.network_reconciliation_counts)})
                    observer = ChromeObserver(browser, servers[case['shard']], output)
                    observer.receipt_authority = receipt_authority
                    await observer.initialize(case)
                return await observer.audit_case(case)
            async def scheduled_audit_one(case):
                return await observe_case_without_capture_contention(case, audit_one, browser_case_lock)
            try:
                async for record in observe_scheduled_cases(assignments[number], scheduled_audit_one, failure_budget):
                    records.append(thin_case_record(record, output))
            except Exception as error:
                worker_errors.append({'worker': number, 'error': safe_error(error)})
            finally:
                if observer:
                    try:
                        async with browser_case_lock:
                            await observer.close()
                    except Exception as error:
                        worker_errors.append({'worker': number, 'stage': 'observer_close', 'error': safe_error(error)})
                    all_forbidden.extend(observer.forbidden_requests); all_sdk.extend(observer.sdk_resources.values())
                    all_task_errors.extend(observer.background_task_errors); all_projection_requests.extend(observer.projection_requests)
                    all_projection_observations.extend(observer.projection_observations)
                    all_sdk_redirects.extend(observer.sdk_redirects)
                    all_loading_failures.extend(observer.network_loading_failures); all_paused_requests.extend(observer.paused_requests)
                    all_starts.extend(observer.network_request_starts); all_extra_infos.extend(observer.network_extra_infos)
                    all_sdk_responses.extend(observer.native_sdk_responses); all_sdk_bodies.extend(observer.native_sdk_body_receipts)
                    all_initial_200.extend(observer.sdk_initial_200.values())
                    all_finished.extend(observer.network_finished); all_response_extra_infos.extend(observer.network_response_extra_infos)
                    all_reconciliation_statistics.append({'cdp_scope_id': observer.cdp_scope_id,
                        'page_target_id': observer.page_target_id, 'counts': deepcopy(observer.network_reconciliation_counts)})

        await asyncio.gather(*(worker(number) for number in range(args.concurrency)))
        try:
            await browser.close()
        except Exception as error:
            worker_errors.append({'stage': 'browser_close', 'error': safe_error(error)})
    # Every context and browser close has completed before the synchronous final
    # witness/index/local memo. No green is reused from a mutable earlier case.
    final_sdk_statistics = finalize_durable_cases(records, output, receipt_authority,
        {'starts': all_starts, 'extra_infos': all_extra_infos,
        'responses': all_sdk_responses, 'bodies': all_sdk_bodies, 'prior_receipts': all_initial_200,
        'finished': all_finished, 'response_extra_infos': all_response_extra_infos}, all_task_errors, all_projection_observations,
        closed_barrier=not worker_errors)
    worker_errors.extend(final_sdk_statistics['errors'])
    all_sdk_refs = [receipt_authority.reference(value, index) for index, value in enumerate(all_sdk)]
    # An incomplete case must not abort the complete raw-ledger closeout. Keep
    # the red stub/error and disclose incompleteness; external expansion refuses
    # it instead of promoting an older green bound file to a terminal record.
    evidence_completeness = {'complete': True, 'errors': []}
    try:
        if validate_evidence_export(records, receipt_authority, all_sdk_refs) is not True:
            raise ValueError('terminal evidence export validation was not explicitly true')
    except Exception as error:
        try:
            diagnostic = {'stage': 'terminal_evidence_incomplete', 'error': safe_error(error)}
        except Exception as formatter_error:
            # Never format either exception again. Type names are bounded,
            # message-free diagnostics; normal receipts remain unchanged.
            original_type = type(error).__name__[:128]
            formatter_type = type(formatter_error).__name__[:128]
            diagnostic = {'stage': 'terminal_evidence_incomplete',
                'error': original_type + ': exception text unavailable', 'error_type': original_type,
                'formatter_failure_type': formatter_type, 'error_text_available': False}
        evidence_completeness = {'complete': False, 'errors': [diagnostic]}
        worker_errors.append(diagnostic)
    try:
        seal_after = verify_evidence_seal(original)
    except Exception as error:
        seal_after = {'passed': False, 'error': safe_error(error)}
    seal_verified = (seal_after.get('passed') is True and seal_before == seal_after
                     and seal_after.get('verified_file_count') == EXPECTED_SEALED_FILES)
    source_after = source_hashes()
    named_after = {name: sha256_file(original / name) for name in named_original}
    observer_after = {name: sha256_file(TOOLING_ROOT / name) for name in observer_files}
    unchanged = source_after == source_before == frozen_source and named_after == named_original and observer_after == observer_files
    if worker_errors or all_forbidden or gate_task_errors(all_task_errors):
        unchanged_for_gate = False
    else:
        unchanged_for_gate = unchanged
    report = {'observer_version': OBSERVER_VERSION, 'observation_only': True, 'chrome_channel': 'chrome',
        'chrome_version': chrome_version, 'provider_requests_authorized': 0, 'network_fulfillment': False,
        'original_named_file_sha256': named_original, 'original_named_file_sha256_after': named_after,
        'observer_files_sha256': observer_files, 'observer_files_sha256_after': observer_after,
        'source_before': source_before, 'source_after': source_after, 'source_unchanged': unchanged,
        'original_evidence_seal_before': seal_before, 'original_evidence_seal_after': seal_after,
        'evidence_schema': COMPACT_EVIDENCE_SCHEMA, 'evidence_completeness': evidence_completeness,
        'evidence_publication_retries': EVIDENCE_PUBLICATION_RETRIES,
        'sdk_receipt_authority': receipt_authority.table(),
        'sdk_final_verification_statistics': final_sdk_statistics,
        'sdk_resources': all_sdk_refs, 'sdk_redirects': all_sdk_redirects, 'forbidden_requests': all_forbidden, 'worker_errors': worker_errors,
        'background_task_errors': all_task_errors, 'historical_session_readonly_projection_enabled': True,
        'network_loading_failed_observations': all_loading_failures, 'fetch_request_paused_observations': all_paused_requests,
        'network_request_start_observations': all_starts, 'network_request_extra_info_observations': all_extra_infos,
        'native_sdk_response_observations': all_sdk_responses, 'native_sdk_actual_body_receipts': all_sdk_bodies,
        'network_loading_finished_observations': all_finished, 'network_response_extra_info_observations': all_response_extra_infos,
        'native_sdk_initial_http200_receipts': all_initial_200,
         'paint_timeout_seconds': PAINT_TIMEOUT_SECONDS, 'browser_case_concurrency': 1,
         'shard_context_workers': args.concurrency, 'max_cases_per_context': MAX_CASES_PER_CONTEXT,
         'cooperative_fail_fast': failure_budget.report(selected),
         'network_reconciliation_statistics': all_reconciliation_statistics,
         'network_reconciliation_statistics_basis': 'actual per-context call counts; raw append precedes conservative O(errors) ID relation; explicit gates always full and uncached; not elapsed-time profiling',
        'historical_session_projection_requests': all_projection_requests, 'stock_session_list_recent_limit': 200,
        'historical_session_projection_responses': all_projection_observations,
        'verified_historical_session_projection_case_count': sum(record['historical_session_readonly_projection_verified'] for record in records),
        'records': records, 'excluded_rasters': excluded, 'selection': selection,
        **coverage_gate([case['map_artifact']['artifact_id'] for case in cases], records,
            raster_excluded=len(excluded), source_unchanged=unchanged_for_gate,
            limit=len(selected) if args.case_ids_file else args.limit,
            background_task_errors=all_task_errors, evidence_seal_verified=seal_verified)}
    report['pilot_passed'] = (args.limit is not None and len(records) == len(selected) and bool(records)
        and all(record['passed'] for record in records) and unchanged and seal_verified
        and not worker_errors and not all_forbidden and not gate_task_errors(all_task_errors))
    report['named_retest_passed'] = (args.case_ids_file is not None and len(records) == len(selected) and bool(records)
        and {record['case_id'] for record in records} == set(selection['case_ids'])
        and all(record['passed'] for record in records) and unchanged and seal_verified
        and not worker_errors and not all_forbidden and not gate_task_errors(all_task_errors)
        and evidence_completeness['complete'])
    write_json(output / 'audit-summary.json', report)
    print(json.dumps({key: report[key] for key in ('full_map_gate', 'pilot_passed', 'expected_map_count',
        'audited_map_count', 'passed_map_count', 'raster_excluded_count', 'source_unchanged')}, ensure_ascii=False))
    return 0 if report['full_map_gate'] or report['pilot_passed'] or report['named_retest_passed'] else 1


def main(argv=None):
    args = parse_cli(argv)
    try:
        return asyncio.run(run_observer(args))
    except Exception as error:
        print(safe_error(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
