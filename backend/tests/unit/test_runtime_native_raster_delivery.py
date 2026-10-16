"""Native GeoTIFF delivery is checked by final typed dataflow, never by op name."""
from __future__ import annotations

import pytest

from app.runtime.compiler import CompileError, Compiler, _requested_export_formats
from app.runtime.contracts import ArtifactRef, PlannerDraft, PlannerStepDraft, TaskOutputRef
from app.runtime.goal_requirements import build_goal_requirements
from app.runtime.llm_planner import OpenAICompatiblePlanner, PlannerRequest, candidate_catalog


CRS = {'kind': 'EPSG', 'code': 3857}
GOAL = '请按先乘后加处理栅格，有效值乘11再加10，无数据保持原样。保留原始坐标系、像元网格和无数据定义，提供最终带地理参考的栅格文件供下载。'


def artifact(kind='Raster'):
    return {'artifact_id': 'source', 'type': kind, 'crs': CRS, 'schema': {},
            'content_hash': 'a' * 64, 'validation_status': 'valid',
            'storage_uri': 'artifacts/sha256/aa/' + 'a' * 64, 'size_bytes': 100}


def calculate(key='calculate'):
    return PlannerStepDraft(task_key=key, op='raster.calculate',
        inputs={'raster': ArtifactRef(artifact_id='source', type='Raster', crs=CRS)},
        params={'expression': 'raster * 11 + 10'})


def compile_goal(goal, tasks, kind='Raster'):
    source = artifact(kind)
    return Compiler(artifact_lookup=lambda identifier: source if identifier == 'source' else None,
                    policy={'trusted_user_goal': goal, 'visible_artifacts': [source]}).compile(
        PlannerDraft(run_id='native-raster-delivery', user_goal=goal, tasks=tasks))


def test_actual_raster_calculation_download_request_compiles_without_vector_export():
    plan = compile_goal(GOAL, [calculate()])
    assert [task.op for task in plan.tasks] == ['raster.calculate']
    assert plan.tasks[-1].outputs[0].artifact_type == 'Raster'
    requirements = build_goal_requirements(GOAL, [artifact()])
    assert requirements['native_raster_delivery']['format'] == 'geotiff'
    assert not any(item['op'] == 'file.export' for item in requirements['operations'])


@pytest.mark.parametrize('format_name', ['TIFF', 'tif', 'GeoTIFF', 'geo-tiff', 'geo tiff'])
def test_explicit_native_tiff_format_uses_the_checked_raster_output(format_name):
    goal = f'栅格有效值乘11再加10，导出为{format_name}格式供下载。'
    plan = compile_goal(goal, [calculate()])
    assert plan.tasks[-1].outputs[0].artifact_type == 'Raster'


@pytest.mark.parametrize('format_name', ['CSV', 'PNG', 'GPKG', 'GeoJSON'])
def test_native_raster_does_not_satisfy_other_explicit_formats(format_name):
    with pytest.raises(CompileError):
        compile_goal(f'栅格有效值乘11再加10，导出为{format_name}格式供下载。', [calculate()])


@pytest.mark.parametrize('format_name', ['PNG', 'JPEG', 'WEBP', 'XYZ'])
@pytest.mark.parametrize('delivery', [
    '导出{format}格式供下载', '提供最终{format}栅格文件供下载',
    '{format}格式下载', '输出{format}', '保存成{format}',
])
def test_named_unsupported_format_cannot_fall_back_to_native_tiff(format_name, delivery):
    goal = '栅格有效值乘11再加10，' + delivery.format(format=format_name) + '。'
    assert _requested_export_formats(goal) == {format_name.lower()}
    with pytest.raises(CompileError) as failure:
        compile_goal(goal, [calculate()])
    assert failure.value.code == 'INVALID_PARAMETER'
    assert failure.value.details['requested_formats'] == [format_name.lower()]


def test_input_image_format_is_not_misread_as_the_requested_output():
    goal = '输入PNG栅格文件。栅格有效值乘11再加10，提供最终GeoTIFF栅格文件供下载。'
    assert _requested_export_formats(goal) == {'geotiff'}
    assert compile_goal(goal, [calculate()]).tasks[-1].outputs[0].artifact_type == 'Raster'


def test_mixed_native_and_vector_formats_do_not_silently_drop_a_delivery():
    with pytest.raises(CompileError):
        compile_goal('栅格有效值乘11再加10，导出为TIFF和GeoJSON格式。', [calculate()])


def test_vector_result_does_not_gain_tiff_delivery_credit():
    task = PlannerStepDraft(task_key='buffer', op='vector.buffer',
        inputs={'vector': ArtifactRef(artifact_id='source', type='Vector', crs=CRS)},
        params={'distance_m': 50})
    with pytest.raises(CompileError):
        compile_goal('生成50米缓冲区，导出为TIFF格式。', [task], kind='Vector')


def test_calculation_cannot_end_in_a_vector_and_claim_final_raster_download():
    polygonize = PlannerStepDraft(task_key='polygonize', op='raster.polygonize',
        inputs={'raster': TaskOutputRef(task_key='calculate', port='raster')})
    with pytest.raises(CompileError):
        compile_goal(GOAL, [calculate(), polygonize])


def test_original_raster_branch_cannot_bypass_required_calculation():
    raw_branch = PlannerStepDraft(task_key='raw', op='raster.reproject',
        inputs={'raster': ArtifactRef(artifact_id='source', type='Raster', crs=CRS)},
        params={'target_crs': CRS})
    with pytest.raises(CompileError, match='GOAL_LINEAGE_MISMATCH'):
        compile_goal(GOAL, [calculate(), raw_branch])


def test_final_raster_must_follow_both_calculation_and_requested_reprojection():
    goal = '栅格有效值乘11再加10，然后把栅格重投影到EPSG:4326，提供最终栅格文件供下载。'
    projected = PlannerStepDraft(task_key='projected', op='raster.reproject',
        inputs={'raster': TaskOutputRef(task_key='calculate', port='raster')},
        params={'target_crs': {'kind': 'EPSG', 'code': 4326}})
    plan = compile_goal(goal, [calculate(), projected])
    assert plan.tasks[-1].outputs[0].crs == {'kind': 'EPSG', 'code': 4326}


def test_native_download_does_not_erase_the_requested_calculation():
    raw = PlannerStepDraft(task_key='raw', op='raster.reproject',
        inputs={'raster': ArtifactRef(artifact_id='source', type='Raster', crs=CRS)},
        params={'target_crs': CRS})
    with pytest.raises(CompileError, match='GOAL_REQUIREMENT_MISSING'):
        compile_goal(GOAL, [raw])


def test_provider_prompt_explains_native_raster_download_without_calling_provider():
    request = PlannerRequest(run_id='raster-hint', input_revision=1, user_goal=GOAL,
                             input_artifacts=(artifact(),), revision_patch={})
    planner = OpenAICompatiblePlanner(api_key='', base_url='', model='', timeout_seconds=1,
                                     max_tokens=32, max_retries=0)
    messages = planner._messages(request, candidate_catalog(planner.registry, request))
    text = '\n'.join(message['content'] for message in messages)
    assert 'GeoTIFF' in text
    assert 'file.export accepts Vector' in text
