import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.tools.raster_analysis import RasterAnalyzer


def test_arithmetic_preserves_nodata_and_georeferencing(tmp_path):
    source = tmp_path / "source.tif"
    output = tmp_path / "output.tif"
    transform = from_origin(100, 200, 10, 10)
    with rasterio.open(source, "w", driver="GTiff", count=1, width=2, height=2, dtype="float32", crs="EPSG:3857", transform=transform, nodata=-9999) as ds:
        ds.write(np.array([[1, -9999], [3, 4]], dtype="float32"), 1)
    result = RasterAnalyzer().raster_calculator({"raster": str(source)}, "raster + 5", str(output))
    assert result["status"] == "success"
    with rasterio.open(output) as ds:
        assert ds.nodata == -9999 and ds.crs.to_epsg() == 3857 and ds.transform == transform
        assert ds.read(1).tolist() == [[6, -9999], [8, 9]]
        assert ds.read_masks(1).tolist() == [[255, 0], [255, 255]]


@pytest.mark.parametrize("expression", ["np.load('anything.npy')", "np.fromfile('anything')", "raster.__class__", "np.lib", "raster.sum()", "[x for x in raster]", "raster + 10**1000000"])
def test_raster_expression_is_a_numeric_dsl_not_general_python(expression):
    with pytest.raises(ValueError, match="RASTER_EXPRESSION_INVALID"):
        RasterAnalyzer._safe_eval(expression, {"raster": np.ones((2, 2))})


def test_allowed_numpy_where_is_elementwise():
    values = np.array([[1, -1], [2, -2]], dtype="float32")
    result = RasterAnalyzer._safe_eval("np.where(raster > 0, raster * 2, 0)", {"raster": values})
    np.testing.assert_array_equal(result, [[2, 0], [4, 0]])


@pytest.mark.parametrize("expression", ["np.where(raster > 0)", "np.sqrt(raster, raster)", "np.round(raster, raster)"])
def test_numeric_functions_cannot_use_index_or_inplace_variants(expression):
    data = np.array([[1, -1], [2, -2]], dtype="float32")
    original = data.copy()
    with pytest.raises(ValueError, match="RASTER_EXPRESSION_INVALID"):
        RasterAnalyzer._safe_eval(expression, {"raster": data})
    np.testing.assert_array_equal(data, original)


def test_round_accepts_bounded_integer_decimals():
    result = RasterAnalyzer._safe_eval("np.round(raster, 2)", {"raster": np.array([[1.2345]])})
    np.testing.assert_allclose(result, [[1.23]])


@pytest.mark.parametrize("nodata,dtype", [(0, "int16"), (-1e100, "float64")])
def test_output_nodata_cannot_collide_with_valid_values_or_overflow(tmp_path, nodata, dtype):
    source, output = tmp_path / "source.tif", tmp_path / "output.tif"
    with rasterio.open(source, "w", driver="GTiff", count=1, width=2, height=2, dtype=dtype, crs="EPSG:3857", transform=from_origin(100, 200, 10, 10), nodata=nodata) as ds:
        ds.write(np.array([[1, nodata], [2, 3]], dtype=dtype), 1)
    result = RasterAnalyzer().raster_calculator({"raster": str(source)}, "raster - 1", str(output))
    assert result["status"] == "success", result
    with rasterio.open(output) as ds:
        assert ds.read_masks(1).tolist() == [[255, 0], [255, 255]]
        assert ds.read(1)[0, 0] == 0
