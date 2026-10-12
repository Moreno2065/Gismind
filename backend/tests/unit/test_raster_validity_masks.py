"""Invalid raster pixels must never become sampled values or polygons."""

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Point

from app.tools.raster_analysis import RasterAnalyzer


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
