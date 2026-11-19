"""Invalid raster pixels must never become sampled values or polygons."""

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.enums import ColorInterp
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds
from shapely.geometry import Point, Polygon

from app.tools.raster_analysis import RasterAnalyzer
from app.runtime.raster_preview import _render


def _raster_with_invalid_pixel(tmp_path, invalid_kind):
    path = tmp_path / f"{invalid_kind}.tif"
    values = np.array([[10, 20], [30, 40]], dtype="float32")
    nodata = None
    if invalid_kind == "nodata":
        values[0, 1] = -9999
        nodata = -9999
    elif invalid_kind == "nan":
        values[0, 1] = np.nan
        nodata = np.nan
    with rasterio.open(
        path, "w", driver="GTiff", height=2, width=2, count=1, dtype="float32",
        crs="EPSG:3857", transform=from_origin(0, 2, 1, 1), nodata=nodata,
    ) as dataset:
        dataset.write(values, 1)
        if invalid_kind == "internal_mask":
            dataset.write_mask(np.array([[255, 0], [255, 255]], dtype="uint8"))
    return path


def test_raster_clip_carries_source_and_polygon_masks_inside_geotiff(tmp_path):
    path = _raster_with_invalid_pixel(tmp_path, "internal_mask")
    boundary = gpd.GeoDataFrame(geometry=[Polygon([(0, 2), (2, 2), (0, 0)])], crs="EPSG:3857")
    output = tmp_path / "clip.tif"
    result = RasterAnalyzer().clip_raster_by_mask(str(path), boundary, str(output))
    assert result["status"] == "success", result
    assert not output.with_suffix(".tif.msk").exists()
    with rasterio.open(output) as clip:
        assert clip.nodata is None
        np.testing.assert_array_equal(clip.read_masks(1), [[255, 0], [255, 0]])


def test_raster_clip_preserves_explicit_mask_precedence_over_nodata(tmp_path):
    source = tmp_path / "masked-nodata.tif"
    values = np.array([[-9999, 3], [4, 8]], dtype="int16")
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(
        source, "w", driver="GTiff", height=2, width=2, count=1, dtype="int16", nodata=-9999,
        crs="EPSG:3857", transform=from_origin(0, 2, 1, 1),
    ) as dataset:
        dataset.write(values, 1)
        dataset.write_mask(np.ones((2, 2), dtype=bool))
    boundary = gpd.GeoDataFrame(geometry=[Polygon([(0, 0), (0, 2), (2, 2), (2, 0)])], crs="EPSG:3857")
    output = tmp_path / "clip.tif"
    result = RasterAnalyzer().clip_raster_by_mask(str(source), boundary, str(output))
    assert result["status"] == "success", result
    with rasterio.open(output) as clip:
        assert clip.nodata == -9999
        np.testing.assert_array_equal(clip.read_masks(1), [[255, 255], [255, 255]])
        np.testing.assert_array_equal(clip.read(1), values)


def test_raster_clip_preserves_rgba_alpha_validity(tmp_path):
    source = tmp_path / "rgba.tif"
    with rasterio.open(source, "w", driver="GTiff", height=2, width=2, count=4, dtype="uint8",
                       crs="EPSG:3857", transform=from_origin(0, 2, 1, 1)) as dataset:
        dataset.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha)
        dataset.write(np.full((3, 2, 2), 100, dtype="uint8"), indexes=[1, 2, 3])
        dataset.write(np.array([[255, 0], [255, 255]], dtype="uint8"), 4)
    boundary = gpd.GeoDataFrame(geometry=[Polygon([(0, 0), (0, 2), (2, 2), (2, 0)])], crs="EPSG:3857")
    output = tmp_path / "clip.tif"
    result = RasterAnalyzer().clip_raster_by_mask(str(source), boundary, str(output))
    assert result["status"] == "success", result
    with rasterio.open(output) as clip:
        assert clip.colorinterp[-1] == ColorInterp.alpha
        np.testing.assert_array_equal(clip.read_masks(1), [[255, 0], [255, 255]])


def test_preview_keeps_positive_grid_and_full_bounds_for_a_projected_single_cell(tmp_path):
    source = tmp_path / "single-cell.tif"
    with rasterio.open(source, "w", driver="GTiff", height=1, width=1, count=1, dtype="int16",
                       crs="EPSG:3857", transform=from_origin(1_000_000, 15_000_000, 100, 100)) as dataset:
        dataset.write(np.array([[42]], dtype="int16"), 1)
        bounds = transform_bounds(dataset.crs, "EPSG:4326", *dataset.bounds)
    preview = _render(str(source), "EPSG:3857")
    assert (preview["width"], preview["height"]) == (1, 1)
    np.testing.assert_allclose(preview["bbox"], bounds, rtol=0, atol=1e-10)
    assert preview["value_range"] == [42, 42]


@pytest.mark.parametrize("invalid_kind", ["nodata", "nan", "internal_mask"])
def test_raster_sampling_returns_null_for_invalid_and_outside_pixels(tmp_path, invalid_kind):
    path = _raster_with_invalid_pixel(tmp_path, invalid_kind)
    points = gpd.GeoDataFrame(
        geometry=[Point(0.5, 1.5), Point(1.5, 1.5), Point(1.5, 0.5), Point(5, 5), Point(2, 1)],
        crs="EPSG:3857",
    )
    result = RasterAnalyzer().raster_sampling(str(path), points)
    assert result["status"] == "success", result
    assert [record["value"] for record in result["data"]] == [10.0, None, 40.0, None, None]


@pytest.mark.parametrize("invalid_kind", ["nodata", "nan", "internal_mask"])
def test_polygonize_excludes_invalid_pixels_from_geometry_and_values(tmp_path, invalid_kind):
    path = _raster_with_invalid_pixel(tmp_path, invalid_kind)
    result = RasterAnalyzer().polygonize_raster(str(path))
    assert result["status"] == "success", result
    frame = result["data"]
    assert frame.crs.to_epsg() == 3857
    assert sorted(frame["value"].tolist()) == [10.0, 30.0, 40.0]
    assert sum(geometry.area for geometry in frame.geometry) == pytest.approx(3.0)
