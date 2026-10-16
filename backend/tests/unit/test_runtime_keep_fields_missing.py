"""Missing requested columns must never turn into a successful lossy projection."""
import copy

import pytest

from app.runtime.contracts import ToolContractRegistry
from app.runtime.postconditions import validate_attribute_keep_fields
from app.runtime.tool_handlers import handle_attribute_keep_fields


def source():
    return {'type': 'FeatureCollection', '_physical_crs': 'EPSG:4326', 'features': [
        {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [118.7, 32.0]},
         'properties': {'名称': '站点甲', '数值': 12, '备注': None}},
    ]}


@pytest.mark.parametrize('fields', [['新名'], ['数值', '新名']])
def test_handler_rejects_missing_requested_columns_without_mutating_input(fields):
    payload = source()
    before = copy.deepcopy(payload)
    result = handle_attribute_keep_fields(input=payload, fields=fields)
    assert result['status'] == 'error'
    assert '新名' in result['message']
    assert payload == before


@pytest.mark.parametrize('fields', [['新名'], ['数值', '新名']])
def test_postcondition_rejects_silent_intersection_with_existing_columns(fields):
    payload = source()
    corrupted = copy.deepcopy(payload)
    corrupted['features'][0]['properties'] = {
        key: value for key, value in payload['features'][0]['properties'].items() if key in fields}
    assert not validate_attribute_keep_fields(
        {'status': 'success', 'data': corrupted}, {'input': payload}, {'fields': fields})


def test_existing_nullable_column_is_not_mistaken_for_missing_column():
    payload = source()
    params = {'fields': ['名称', '备注']}
    result = handle_attribute_keep_fields(input=payload, **params)
    assert result['status'] == 'success'
    assert result['data']['features'][0]['properties'] == {'名称': '站点甲', '备注': None}
    assert validate_attribute_keep_fields(result, {'input': payload}, params)


def test_missing_column_semantics_invalidate_old_executable_cache_identity():
    assert ToolContractRegistry.with_builtins().get('attribute.keep_fields').version == '1.2.0'
