"""Synthetic negative controls for a separately versioned pixel observer.

These are oracle unit tests, never real-provider success evidence.
"""
import importlib.util
from pathlib import Path

import numpy as np
import pytest


def oracle():
    path = Path(__file__).with_name("visible_paint_oracle.py")
    assert path.is_file(), "independent visible-paint oracle not implemented"
    spec = importlib.util.spec_from_file_location("audit_visible_paint", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verify_visible_paint


def scene():
    hidden = np.full((80, 100, 3), 220, dtype=np.uint8)
    core = np.zeros(hidden.shape[:2], dtype=bool)
    core[25:50, 30:60] = True
    contour = np.zeros_like(core)
    contour[23, 28:62] = True
    contour[51, 28:62] = True
    contour[23:52, 28] = True
    contour[23:52, 61] = True
    allowed = np.zeros_like(core)
    allowed[19:56, 24:66] = True
    shown = hidden.copy()
    shown[core] = 195
    shown[contour] = 140
    return hidden, shown, core, contour, allowed


def check(hidden=None, shown=None, hidden_again=None, shown_again=None, **kwargs):
    base, paint, core, contour, allowed = scene()
    return oracle()(
        base if hidden is None else hidden,
        paint if shown is None else shown,
        base if hidden_again is None else hidden_again,
        paint if shown_again is None else shown_again,
        core_mask=kwargs.pop("core_mask", core),
        contour_mask=kwargs.pop("contour_mask", contour),
        allowed_mask=kwargs.pop("allowed_mask", allowed),
        **kwargs,
    )


def test_repeatable_full_fill_and_contour_are_observed():
    result = check()
    assert result["passed"] is True
    assert result["core_coverage"] == 1
    assert result["contour_coverage"] == 1
    assert result["outside_noise_fraction"] == 0


def test_point_core_can_be_verified_without_polygon_contour():
    result = check(contour_mask=None)
    assert result["passed"] is True
    assert result["contour_coverage"] is None


def test_blank_grid_or_transparent_overlay_does_not_pass():
    hidden, _, _, _, _ = scene()
    with pytest.raises(ValueError, match="core coverage"):
        check(shown=hidden, shown_again=hidden)


def test_one_changed_pixel_is_not_visible_geometry_proof():
    hidden, _, _, _, _ = scene()
    one = hidden.copy()
    one[35, 40] = 140
    with pytest.raises(ValueError, match="core coverage"):
        check(shown=one, shown_again=one)


def test_stroke_without_required_fill_is_rejected():
    hidden, _, _, contour, _ = scene()
    stroke = hidden.copy()
    stroke[contour] = 140
    with pytest.raises(ValueError, match="core coverage"):
        check(shown=stroke, shown_again=stroke)


def test_required_contour_is_not_replaced_by_interior_change():
    hidden, _, core, _, _ = scene()
    fill = hidden.copy()
    fill[core] = 195
    with pytest.raises(ValueError, match="contour coverage"):
        check(shown=fill, shown_again=fill)


def test_full_polygon_fill_cannot_impersonate_the_requested_stroke():
    hidden, _, _, _, _ = scene()
    fill = hidden.copy()
    fill[23:52, 28:62] = 195
    with pytest.raises(ValueError, match="contour coverage"):
        check(shown=fill, shown_again=fill)


def test_sparse_stroke_dots_are_not_a_continuous_perimeter():
    hidden, _, core, _, _ = scene()
    sparse = np.zeros_like(core)
    sparse[23, 28:62:3] = True
    sparse[51, 28:62:3] = True
    sparse[23:52:3, 28] = True
    sparse[23:52:3, 61] = True
    shown = hidden.copy()
    shown[core] = 195
    shown[sparse] = 140
    with pytest.raises(ValueError, match="raw contour coverage"):
        check(shown=shown, shown_again=shown)


def test_local_perimeter_gap_cannot_hide_in_global_coverage():
    hidden, shown, _, _, _ = scene()
    shown[23, 30:35] = hidden[23, 30:35]
    with pytest.raises(ValueError, match="local contour coverage"):
        check(shown=shown, shown_again=shown)


def test_background_change_cannot_impersonate_an_overlay():
    hidden, shown, _, _, _ = scene()
    hidden_again = hidden.copy()
    hidden_again[25:50, 30:60] = 170
    with pytest.raises(ValueError, match="hidden background unstable"):
        check(hidden_again=hidden_again, shown=shown)


def test_basemap_tile_change_outside_geometry_invalidates_measurement():
    _, shown, _, _, _ = scene()
    noisy = shown.copy()
    noisy[0:15, 0:20] = 80
    with pytest.raises(ValueError, match="outside geometry noise"):
        check(shown=noisy, shown_again=noisy)


def test_different_repeat_show_shape_is_rejected():
    _, shown, _, _, _ = scene()
    changed = shown.copy()
    changed[25:50, 30:45] = 220
    with pytest.raises(ValueError, match="shown image unstable"):
        check(shown_again=changed)


def test_very_small_core_is_unproved_not_green():
    _, _, core, _, _ = scene()
    core[:] = False
    core[30:32, 40:42] = True
    with pytest.raises(ValueError, match="insufficient core pixels"):
        check(core_mask=core)


def test_geometry_at_crop_edge_is_rejected():
    _, _, core, _, allowed = scene()
    core[0, 40] = True
    allowed[0, 40] = True
    with pytest.raises(ValueError, match="core touches crop edge"):
        check(core_mask=core, allowed_mask=allowed)


def test_allowed_extent_must_include_full_expected_geometry():
    _, _, core, _, allowed = scene()
    allowed[core] = False
    with pytest.raises(ValueError, match="allowed mask excludes"):
        check(allowed_mask=allowed)


@pytest.mark.parametrize("dtype", [np.float32, np.uint16])
def test_non_rgb8_images_are_rejected(dtype):
    hidden, _, _, _, _ = scene()
    with pytest.raises(ValueError, match="uint8 RGB"):
        check(hidden=hidden.astype(dtype))


def test_mismatched_image_shapes_are_rejected():
    hidden, _, _, _, _ = scene()
    with pytest.raises(ValueError, match="same shape"):
        check(hidden=hidden[:-1])


def test_mask_must_be_boolean_and_exact_image_shape():
    _, _, core, _, _ = scene()
    with pytest.raises(ValueError, match="boolean mask"):
        check(core_mask=core.astype(np.uint8))
    with pytest.raises(ValueError, match="mask shape"):
        check(core_mask=core[:-1])


@pytest.mark.parametrize("value", [0, -0.1, 1.1, float("nan"), True])
def test_invalid_coverage_threshold_is_rejected(value):
    with pytest.raises(ValueError, match="coverage threshold"):
        check(min_core_coverage=value)


@pytest.mark.parametrize("value", [-0.1, 0.5, float("nan"), True])
def test_noise_threshold_cannot_disable_stability_guard(value):
    with pytest.raises(ValueError, match="noise threshold"):
        check(max_noise_fraction=value)


def test_outside_noise_check_can_allow_only_bounded_roundoff():
    _, shown, _, _, _ = scene()
    slight = shown.copy()
    slight[5, 5] = 218
    result = check(shown=slight, shown_again=slight)
    assert result["passed"] is True


def test_masks_without_reference_background_are_rejected():
    _, _, core, _, allowed = scene()
    allowed[:] = True
    with pytest.raises(ValueError, match="insufficient outside reference"):
        check(allowed_mask=allowed, core_mask=core)


@pytest.mark.parametrize("phase", ["hidden", "shown"])
def test_local_marker_drift_is_not_diluted_by_a_large_image(phase):
    hidden = np.full((1000, 1000, 3), 220, dtype=np.uint8)
    core = np.zeros((1000, 1000), dtype=bool)
    core[490:500, 490:500] = True
    allowed = np.zeros_like(core)
    allowed[485:505, 485:505] = True
    shown = hidden.copy()
    shown[core] = 195
    hidden_again = hidden.copy()
    shown_again = shown.copy()
    if phase == "hidden":
        hidden_again[core] = 200
    else:
        shown_again[core] = 180
    with pytest.raises(ValueError, match=f"{phase} .*unstable"):
        oracle()(hidden, shown, hidden_again, shown_again,
                 core_mask=core, contour_mask=None, allowed_mask=allowed)
