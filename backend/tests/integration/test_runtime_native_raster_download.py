"""Real uploaded GeoTIFF -> compiled processing -> isolated worker -> download."""
from __future__ import annotations

import json
import os

import numpy as np
import rasterio
from rasterio.transform import from_origin

from app.runtime import ArtifactRef, Compiler, PlannerDraft, PlannerStepDraft, RuntimeSupervisor, TaskOutputRef
from tests.integration.test_runtime_upload_formats import client, _upload


def test_native_calculated_raster_is_the_actual_download_without_vector_export(client, tmp_path):
    source = tmp_path / 'source.tif'
    values = np.array([[1, -9999], [3, 4]], dtype='float32')
    transform = from_origin(1000, 2000, 10, 10)
    with rasterio.open(source, 'w', driver='GTiff', height=2, width=2, count=1,
                       dtype='float32', crs='EPSG:3857', transform=transform, nodata=-9999) as dataset:
        dataset.write(values, 1)
    response = _upload(client, 'source.tif', source.read_bytes())
    assert response.status_code == 201, response.text
    uploaded = response.json()
    runtime = client.app.state.runtime_store
    artifacts = client.app.state.artifact_store
    goal = '栅格有效值乘11再加10，无数据保持原样，提供最终栅格文件并导出为TIFF格式供下载。'
    plan = Compiler(artifact_lookup=runtime.get_artifact,
        policy={'trusted_user_goal': goal, 'visible_artifacts': [uploaded]}).compile(
        PlannerDraft(run_id='native-raster', user_goal=goal, tasks=[
            PlannerStepDraft(task_key='load', op='io.load_raster', inputs={'file':
                ArtifactRef(artifact_id=uploaded['artifact_id'], type='File', crs=uploaded['crs'])}),
            PlannerStepDraft(task_key='calculate', op='raster.calculate', inputs={'raster':
                TaskOutputRef(task_key='load', port='raster')}, params={'expression': 'raster * 11 + 10'}),
        ]))
    assert [task.op for task in plan.tasks] == ['io.load_raster', 'raster.calculate']
    runtime.create_run('native-raster', 'uploads')
    runtime.create_plan('native-plan', 'native-raster', plan=plan)
    runtime.create_task('native-load', 'native-plan', 'load', tool_name='io.load_raster')
    runtime.create_task('native-calculate', 'native-plan', 'calculate', tool_name='raster.calculate', status='blocked')
    runtime.add_task_dependency('native-calculate', 'native-load')
    runtime.bind_task_input('native-raster-input', 'native-calculate', 'raster',
                            source_task_id='native-load', source_port='raster')
    worker = RuntimeSupervisor(runtime, artifacts, isolate=True, autostart=False)
    assert worker.run_once('native-download', run_id='native-raster')
    result = worker.run_once('native-download', run_id='native-raster')
    assert runtime.get_run('native-raster')['status'] == 'completed'
    final = result['artifact']
    assert final['type'] == 'Raster' and final['validation_status'] == 'valid'
    downloaded = client.get(f"/api/artifacts/{final['artifact_id']}/content")
    assert downloaded.status_code == 200
    assert downloaded.content == artifacts.path_for(final['artifact_id']).read_bytes()
    with rasterio.MemoryFile(downloaded.content) as memory:
        with memory.open() as actual:
            assert actual.driver == 'GTiff'
            assert actual.crs.to_epsg() == 3857 and actual.transform == transform
            assert actual.nodata == -9999
            np.testing.assert_array_equal(actual.read(1), np.array([[21, -9999], [43, 54]], dtype='float32'))
    identities = runtime._read(lambda connection: [json.loads(row[0]) for row in connection.execute(
        "SELECT executor_identity FROM task_attempts WHERE status='succeeded'")])
    assert len(identities) == 2 and all(item['pid'] != os.getpid() for item in identities)
