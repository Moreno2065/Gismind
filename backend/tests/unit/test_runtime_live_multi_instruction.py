"""The physical live oracle must reject partially completed multi-step work."""
import geopandas as gpd
import pytest
from shapely.geometry import Point

from tests.browser.runtime_live_browser_support import CASES, fixture_uploads, verify_download


def test_live_inventory_contains_real_multi_instruction_case():
    assert "multi_step" in CASES
    assert "重命名" in CASES["multi_step"]["prompt"]
    assert fixture_uploads("multi_step")[0][1]["features"][0]["properties"] == {"site_id": "S1"}


def test_multi_instruction_oracle_requires_rename_and_buffer(tmp_path):
    local = "+proj=aeqd +lat_0=32.05 +lon_0=118.8 +datum=WGS84 +units=m"
    frame = gpd.GeoDataFrame({"site_id": ["S1"]}, geometry=[Point(0, 0).buffer(500)], crs=local).to_crs(4326)
    wrong = tmp_path / "missing-rename.gpkg"
    frame.to_file(wrong, driver="GPKG")
    with pytest.raises(AssertionError, match="rename"):
        verify_download("multi_step", wrong)
    correct = tmp_path / "all-steps.gpkg"
    frame.rename(columns={"site_id": "站点编号"}).to_file(correct, driver="GPKG")
    assert verify_download("multi_step", correct)["feature_count"] == 1
