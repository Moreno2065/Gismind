"""Independent causal-pixel checks; not an LLM or application implementation.

The caller provides masks projected from independently read physical GIS data.
Four real, isolated-overlay captures must be hide/show/hide/show, with the
view, geometry and styles unchanged. No basemap-completion claim is made.
"""
import math
import numbers

import numpy as np


def _changed(left, right, delta):
    return _intensity(left, right) >= delta


def _intensity(left, right):
    return np.max(np.abs(left.astype(np.int16) - right.astype(np.int16)), axis=2)


def _dilate(mask, radius=1):
    height, width = mask.shape
    padded = np.pad(mask, radius, constant_values=False)
    result = np.zeros_like(mask)
    for dy in range(2 * radius + 1):
        for dx in range(2 * radius + 1):
            result |= padded[dy:dy + height, dx:dx + width]
    return result


def _threshold(value, name, *, lower, upper, inclusive_lower=False):
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ValueError(f"invalid {name}")
    value = float(value)
    lower_ok = value >= lower if inclusive_lower else value > lower
    if not math.isfinite(value) or not lower_ok or value > upper:
        raise ValueError(f"invalid {name}")
    return value


def verify_visible_paint(
    hidden_before,
    shown_first,
    hidden_after,
    shown_again,
    *,
    core_mask,
    contour_mask=None,
    allowed_mask,
    min_core_coverage=0.70,
    min_contour_coverage=0.95,
    max_noise_fraction=0.002,
    channel_delta=3,
    stroke_color=(138, 150, 166),
):
    """Return quantitative evidence, or fail closed on unproved visible paint.

    Core is polygon interior or marker interior. Contour is a thin independently
    projected polygon perimeter; one-pixel matching handles rasterization, not
    geographic-coordinate differences. Allowed extent bounds stroke/marker glow.
    """
    core_threshold = _threshold(min_core_coverage, "coverage threshold", lower=0, upper=1)
    contour_threshold = _threshold(min_contour_coverage, "coverage threshold", lower=0, upper=1)
    noise_threshold = _threshold(
        max_noise_fraction, "noise threshold", lower=0, upper=0.02, inclusive_lower=True,
    )
    if isinstance(channel_delta, bool) or not isinstance(channel_delta, int) or not 1 <= channel_delta <= 255:
        raise ValueError("invalid RGB channel delta")
    if (not isinstance(stroke_color, (tuple, list)) or len(stroke_color) != 3
            or any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 255 for v in stroke_color)):
        raise ValueError("invalid independent stroke RGB color")
    images = (hidden_before, shown_first, hidden_after, shown_again)
    for image in images:
        if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("images must be uint8 RGB arrays")
    if any(image.shape != images[0].shape for image in images):
        raise ValueError("images must have the same shape")
    shape = images[0].shape[:2]
    for mask in (core_mask, contour_mask, allowed_mask):
        if mask is None and mask is contour_mask:
            continue
        if not isinstance(mask, np.ndarray) or mask.dtype != bool:
            raise ValueError("expected a boolean mask")
        if mask.shape != shape:
            raise ValueError("mask shape differs from image shape")
    core_count = int(np.count_nonzero(core_mask))
    if core_count < 25:
        raise ValueError("insufficient core pixels: visible paint is unproved")
    if np.any(core_mask[0]) or np.any(core_mask[-1]) or np.any(core_mask[:, 0]) or np.any(core_mask[:, -1]):
        raise ValueError("core touches crop edge")
    expected = core_mask if contour_mask is None else core_mask | contour_mask
    if np.any(expected & ~allowed_mask):
        raise ValueError("allowed mask excludes expected geometry")
    outside = ~allowed_mask
    outside_count = int(np.count_nonzero(outside))
    if outside_count < 100:
        raise ValueError("insufficient outside reference background")
    hidden_change = _changed(hidden_before, hidden_after, channel_delta)
    hidden_drift = float(np.mean(hidden_change))
    hidden_local_drift = float(np.mean(hidden_change[allowed_mask]))
    if max(hidden_drift, hidden_local_drift) > noise_threshold:
        raise ValueError(f"hidden background unstable: global={hidden_drift:.6f}, local={hidden_local_drift:.6f}")
    shown_change = _changed(shown_first, shown_again, channel_delta)
    shown_drift = float(np.mean(shown_change))
    shown_local_drift = float(np.mean(shown_change[allowed_mask]))
    if max(shown_drift, shown_local_drift) > noise_threshold:
        raise ValueError(f"shown image unstable: global={shown_drift:.6f}, local={shown_local_drift:.6f}")
    first_change = _changed(hidden_before, shown_first, channel_delta)
    second_change = _changed(hidden_after, shown_again, channel_delta)
    outside_noise = float(np.count_nonzero((first_change | second_change) & outside) / outside_count)
    if outside_noise > noise_threshold:
        raise ValueError(f"outside geometry noise: {outside_noise:.6f}")
    causal_change = first_change & second_change
    core_coverage = float(np.count_nonzero(causal_change & core_mask) / core_count)
    if core_coverage < core_threshold:
        raise ValueError(f"insufficient core coverage: {core_coverage:.6f}")
    contour_coverage = None
    raw_contour_coverage = None
    minimum_local_contour_coverage = None
    if contour_mask is not None:
        contour_count = int(np.count_nonzero(contour_mask))
        if contour_count < 8:
            raise ValueError("insufficient contour pixels: visible paint is unproved")
        # The default 0.24 fill cannot stand in for the requested opaque stroke.
        # Contrast is relative to the real hidden basemap and the independently
        # known renderer color, not a heuristic derived from observed fill.
        color = np.array(stroke_color, dtype=np.int16)
        first_contrast = np.max(np.abs(hidden_before.astype(np.int16) - color), axis=2)
        second_contrast = np.max(np.abs(hidden_after.astype(np.int16) - color), axis=2)
        strong_stroke = (
            causal_change
            & (_intensity(hidden_before, shown_first) >= np.maximum(channel_delta, first_contrast * 0.45))
            & (_intensity(hidden_after, shown_again) >= np.maximum(channel_delta, second_contrast * 0.45))
        )
        raw_contour_coverage = float(np.count_nonzero(strong_stroke & contour_mask) / contour_count)
        if raw_contour_coverage < 0.70:
            raise ValueError(f"insufficient raw contour coverage: {raw_contour_coverage:.6f}")
        raster_matched_stroke = _dilate(strong_stroke)
        contour_coverage = float(np.count_nonzero(raster_matched_stroke & contour_mask) / contour_count)
        if contour_coverage < contour_threshold:
            raise ValueError(f"insufficient contour coverage: {contour_coverage:.6f}")
        ys, xs = np.nonzero(contour_mask)
        local_coverages = []
        # Independent spatial perimeter cells reject a contiguous missing arc
        # which a whole-perimeter mean could otherwise hide.
        xbins = np.minimum(3, (xs - xs.min()) * 4 // max(1, int(xs.max() - xs.min() + 1)))
        ybins = np.minimum(3, (ys - ys.min()) * 4 // max(1, int(ys.max() - ys.min() + 1)))
        for cell in range(16):
            members = (ybins * 4 + xbins) == cell
            if int(np.count_nonzero(members)) >= 3:
                local_coverages.append(float(np.mean(raster_matched_stroke[ys[members], xs[members]])))
        minimum_local_contour_coverage = min(local_coverages, default=0.0)
        if minimum_local_contour_coverage < 0.90:
            raise ValueError(f"insufficient local contour coverage: {minimum_local_contour_coverage:.6f}")
    return {
        "passed": True,
        "core_coverage": core_coverage,
        "contour_coverage": contour_coverage,
        "raw_contour_coverage": raw_contour_coverage,
        "minimum_local_contour_coverage": minimum_local_contour_coverage,
        "outside_noise_fraction": outside_noise,
        "hidden_drift_fraction": hidden_drift,
        "shown_drift_fraction": shown_drift,
        "hidden_local_drift_fraction": hidden_local_drift,
        "shown_local_drift_fraction": shown_local_drift,
        "core_pixels": core_count,
        "changed_core_pixels": int(np.count_nonzero(causal_change & core_mask)),
        "parameters": {
            "channel_delta": channel_delta,
            "min_core_coverage": core_threshold,
            "min_contour_coverage": contour_threshold,
            "max_noise_fraction": noise_threshold,
            "contour_raster_tolerance_pixels": 1,
            "stroke_rgb": list(stroke_color),
            "minimum_stroke_contrast_fraction": 0.45,
            "minimum_raw_contour_coverage": 0.70,
            "minimum_local_contour_coverage": 0.90,
            "contour_grid_dimension": 4,
        },
    }
