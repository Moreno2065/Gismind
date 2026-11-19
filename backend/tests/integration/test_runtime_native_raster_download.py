"""Real uploaded GeoTIFF -> compiled processing -> isolated worker -> download."""
from __future__ import annotations

import base64
import io
import json
import os

import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds
from shapely.geometry import Polygon, mapping

from app.runtime import ArtifactRef, Compiler, PlannerDraft, PlannerStepDraft, RuntimeSupervisor, TaskOutputRef
from tests.integration.test_runtime_upload_formats import client, _upload


def test_clipped_dem_preview_distinguishes_zero_from_masked_pixels(client, tmp_path):
    source = tmp_path / 'dem.tif'
    pixel = 1 / 64
    transform = from_origin(118, 33, pixel, pixel)
    values = np.arange(16, dtype='uint16').reshape(4, 4)
    with rasterio.open(source, 'w', driver='GTiff', width=4, height=4, count=1,
                       dtype='uint16', crs='EPSG:4326', transform=transform) as dataset:
        dataset.write(values, 1)
    east, south = 118 + 3 * pixel, 33 - 3 * pixel
    outline = Polygon([(118, 33), (east, 33), (east, 33 - pixel),
                       (118 + pixel, 33 - pixel), (118 + pixel, south), (118, south)])
    boundary = json.dumps({'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'geometry': mapping(outline), 'properties': {}}
    ]}).encode()
    uploads = [_upload(client, 'dem.tif', source.read_bytes()), _upload(client, 'boundary.geojson', boundary)]
    assert [response.status_code for response in uploads] == [201, 201]
    dem, mask = [response.json() for response in uploads]
    def preview_url(item):
        return f"/api/artifacts/{item['artifact_id']}/raster-preview"

    input_preview = client.get(preview_url(dem))
    assert input_preview.status_code == 200, input_preview.text
    assert input_preview.json()['value_range'] == [0, 15]
    assert client.get(preview_url(mask)).status_code == 422

    runtime = client.app.state.runtime_store
    artifacts = client.app.state.artifact_store
    goal = '按上传的矢量边界裁剪 DEM，交付 GeoTIFF。'
    plan = Compiler(artifact_lookup=runtime.get_artifact,
        policy={'trusted_user_goal': goal, 'visible_artifacts': [dem, mask]}).compile(
        PlannerDraft(run_id='dem-preview', user_goal=goal, tasks=[
            PlannerStepDraft(task_key='dem', op='io.load_raster', inputs={'file':
                ArtifactRef(artifact_id=dem['artifact_id'], type='File', crs=dem['crs'])}),
            PlannerStepDraft(task_key='boundary', op='io.load_vector', inputs={'file':
                ArtifactRef(artifact_id=mask['artifact_id'], type='File', crs=mask['crs'])}),
            PlannerStepDraft(task_key='clip', op='raster.clip_mask', inputs={
                'raster': TaskOutputRef(task_key='dem', port='raster'),
                'mask': TaskOutputRef(task_key='boundary', port='vector'),
            }),
        ]))
    runtime.create_run('dem-preview', 'uploads')
    runtime.create_plan('dem-plan', 'dem-preview', plan=plan)
    runtime.create_task('dem-load', 'dem-plan', 'dem', tool_name='io.load_raster')
    runtime.create_task('dem-boundary', 'dem-plan', 'boundary', tool_name='io.load_vector')
    runtime.create_task('dem-clip', 'dem-plan', 'clip', tool_name='raster.clip_mask', status='blocked')
    for source_task, port, output_port in [('dem-load', 'raster', 'raster'), ('dem-boundary', 'mask', 'vector')]:
        runtime.add_task_dependency('dem-clip', source_task)
        runtime.bind_task_input(f'dem-clip-{port}', 'dem-clip', port,
                                source_task_id=source_task, source_port=output_port)
    worker = RuntimeSupervisor(runtime, artifacts, isolate=True, autostart=False)
    assert worker.run_once('preview-test', run_id='dem-preview')
    assert worker.run_once('preview-test', run_id='dem-preview')
    final = worker.run_once('preview-test', run_id='dem-preview')['artifact']
    assert runtime.get_run('dem-preview')['status'] == 'completed'

    result = client.get(preview_url(final))
    assert result.status_code == 200, result.text
    preview = result.json()
    assert preview['artifact_id'] == final['artifact_id']
    assert preview['crs'] == {'kind': 'WGS84'}
    np.testing.assert_allclose(preview['bbox'], [118, south, east, 33])
    assert preview['band'] == preview['band_count'] == 1
    assert preview['nodata'] is None and preview['value_range'] == [0, 8]
    alpha = np.array(Image.open(io.BytesIO(base64.b64decode(preview['png_b64']))).getchannel('A'))
    expected_valid = np.array([[255, 255, 255], [255, 0, 0], [255, 0, 0]], dtype='uint8')
    np.testing.assert_array_equal(alpha, expected_valid)

    download = client.get(f"/api/artifacts/{final['artifact_id']}/content")
    assert download.status_code == 200
    assert download.content == artifacts.path_for(final['artifact_id']).read_bytes()
    with rasterio.MemoryFile(download.content) as memory, memory.open() as actual:
        assert actual.driver == 'GTiff' and actual.crs.to_epsg() == 4326
        assert actual.transform == transform and actual.nodata is None
        np.testing.assert_array_equal(actual.read_masks(1), expected_valid)
        np.testing.assert_array_equal(actual.read(1)[expected_valid > 0], [0, 1, 2, 4, 8])


def test_projected_upload_preview_is_bounded_and_respects_declared_nodata(client, tmp_path):
    source = tmp_path / 'projected.tif'
    values = np.zeros((640, 1024), dtype='int16')
    values[:, :512] = -9999
    values[:320, 512:] = 800
    with rasterio.open(source, 'w', driver='GTiff', width=1024, height=640, count=1,
                       dtype='int16', crs='EPSG:3857', transform=from_origin(13_000_000, 4_000_000, 30, 30),
                       nodata=-9999) as dataset:
        dataset.write(values, 1)
        expected_bounds = transform_bounds(dataset.crs, 'EPSG:4326', *dataset.bounds)

    upload = _upload(client, 'projected.tif', source.read_bytes())
    assert upload.status_code == 201, upload.text
    artifact = upload.json()
    result = client.get(f"/api/artifacts/{artifact['artifact_id']}/raster-preview")
    assert result.status_code == 200, result.text
    preview = result.json()
    assert preview['width'] == 512 and 1 <= preview['height'] < 512
    assert preview['value_range'] == [0, 800] and float(preview['nodata']) == -9999
    np.testing.assert_allclose(preview['bbox'], expected_bounds, rtol=0, atol=.001)
    alpha = np.array(Image.open(io.BytesIO(base64.b64decode(preview['png_b64']))).getchannel('A'))
    assert (alpha[:, :200] == 0).all() and (alpha[:, -200:] == 255).all()
    assert client.get(artifact['content_url']).content == source.read_bytes()


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
