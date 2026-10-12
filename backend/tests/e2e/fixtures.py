"""Deterministic spatial fixtures built at test time.

These are generated in the test process instead of being loaded from golden
files: every case therefore exercises only the documented numerical contract of
the Runtime and has no historical artifact dependency.
"""

from __future__ import annotations

import io
import json
import math
from typing import Any

WGS84 = {"kind": "EPSG", "code": 4326}
GCJ02 = {"kind": "GCJ02"}
PROJECTED_3857 = {"kind": "EPSG", "code": 3857}
CGCS2000_3DEGREE = {"kind": "EPSG", "code": 4545}

CATEGORIES = ("hospital", "school", "park", "mall", "station")


def _feature(geometry: dict[str, Any], index: int, *, with_props: bool = True) -> dict[str, Any]:
    if not with_props:
        return {"type": "Feature", "geometry": geometry, "properties": {}}
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "name": f"poi-{index}",
            "category": CATEGORIES[index % len(CATEGORIES)],
            "value": 10 + index * 5,
            "score": round(1.0 + index * 0.25, 3),
            "city": "南京" if index % 2 == 0 else "北京",
        },
    }


def point(lon: float, lat: float) -> dict[str, Any]:
    return {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]}


def ring(lon: float, lat: float, size: float) -> list[list[float]]:
    return [
        [round(lon, 6), round(lat, 6)],
        [round(lon + size, 6), round(lat, 6)],
        [round(lon + size, 6), round(lat + size, 6)],
        [round(lon, 6), round(lat + size, 6)],
        [round(lon, 6), round(lat, 6)],
    ]


def polygon(lon: float, lat: float, size: float) -> dict[str, Any]:
    return {"type": "Polygon", "coordinates": [ring(lon, lat, size)]}


def line(lon: float, lat: float, size: float) -> dict[str, Any]:
    return {
        "type": "LineString",
        "coordinates": [
            [round(lon, 6), round(lat, 6)],
            [round(lon + size / 2, 6), round(lat + size / 2, 6)],
            [round(lon + size, 6), round(lat, 6)],
        ],
    }


def multipolygon(lon: float, lat: float, size: float) -> dict[str, Any]:
    return {
        "type": "MultiPolygon",
        "coordinates": [[ring(lon, lat, size)], [ring(lon + 2 * size, lat, size)]],
    }


def geojson(
    geometries: list[dict[str, Any]],
    *,
    with_props: bool = True,
    crs_label: str | None = None,
    extra: dict[str, Any] | None = None,
) -> bytes:
    payload: dict[str, Any] = {
        "type": "FeatureCollection",
        "features": [_feature(geometry, index, with_props=with_props) for index, geometry in enumerate(geometries)],
    }
    if crs_label == "GCJ02":
        payload["_crs_label"] = "GCJ02"
        payload["_physical_crs"] = "EPSG:4326"
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def points(count: int = 5, *, lon0: float = 118.75, lat0: float = 32.03, step: float = 0.01, **kwargs) -> bytes:
    return geojson([point(lon0 + index * step, lat0 + index * step) for index in range(count)], **kwargs)


def polygons(count: int = 3, *, lon0: float = 118.75, lat0: float = 32.03, step: float = 0.02, size: float = 0.01, **kwargs) -> bytes:
    return geojson([polygon(lon0 + index * step, lat0 + index * step, size) for index in range(count)], **kwargs)


def lines(count: int = 3, *, lon0: float = 118.75, lat0: float = 32.03, step: float = 0.02, size: float = 0.01, **kwargs) -> bytes:
    return geojson([line(lon0 + index * step, lat0 + index * step, size) for index in range(count)], **kwargs)


def mixed(**kwargs) -> bytes:
    return geojson(
        [point(118.75, 32.03), polygon(118.78, 32.05, 0.01), line(118.80, 32.07, 0.01)],
        **kwargs,
    )


def multipolygons(count: int = 2, **kwargs) -> bytes:
    return geojson([multipolygon(118.75 + index * 0.05, 32.03, 0.01) for index in range(count)], **kwargs)


def duplicate_geometries(count: int = 6, **kwargs) -> bytes:
    """Three distinct shapes, each repeated twice -> delete_duplicates yields 3."""
    shapes = [polygon(118.75, 32.03, 0.01), polygon(118.79, 32.06, 0.01), point(118.82, 32.09)]
    geometries = [shapes[index % len(shapes)] for index in range(count)]
    return geojson(geometries, **kwargs)


def bowtie(**kwargs) -> bytes:
    """A self-intersecting polygon: invalid geometry for fix_geometries/check_validity."""
    return geojson(
        [
            {
                "type": "Polygon",
                "coordinates": [[[118.75, 32.03], [118.78, 32.06], [118.78, 32.03], [118.75, 32.06], [118.75, 32.03]]],
            }
        ],
        **kwargs,
    )


def empty(**kwargs) -> bytes:
    return geojson([], **kwargs)


def single_point(**kwargs) -> bytes:
    return geojson([point(118.75, 32.03)], **kwargs)


def geotiff(
    width: int = 24,
    height: int = 24,
    *,
    bands: int = 1,
    nodata: float | None = -9999.0,
    lon0: float = 118.70,
    lat0: float = 32.10,
    resolution: float = 0.001,
    crs: str = "EPSG:4326",
    dtype: str = "float32",
) -> bytes:
    """A synthetic DEM: a smooth dome with a distinct peak, plus a nodata rim."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    rows = []
    for row in range(height):
        values = []
        for col in range(width):
            dx = (col - width / 2) / (width / 2)
            dy = (row - height / 2) / (height / 2)
            values.append(float(1000.0 * math.exp(-(dx * dx + dy * dy)) + 50.0))
        rows.append(values)
    array = np.asarray(rows, dtype=dtype)
    if nodata is not None:
        array[0, 0] = nodata
    if bands > 1:
        array = np.stack([array * (index + 1) for index in range(bands)])

    buffer = io.BytesIO()
    with rasterio.open(
        buffer,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=bands,
        dtype=dtype,
        crs=crs,
        transform=from_origin(lon0, lat0, resolution, resolution),
        nodata=nodata,
    ) as dataset:
        if bands == 1:
            dataset.write(array, 1)
        else:
            for band in range(bands):
                dataset.write(array[band], band + 1)
    return buffer.getvalue()


def csv_bytes(
    rows: int = 12,
    *,
    x_col: str = "lon",
    y_col: str = "lat",
    extra_cols: bool = True,
    bad_rows: int = 0,
) -> bytes:
    header = [x_col, y_col]
    if extra_cols:
        header += ["name", "value"]
    lines = [",".join(header)]
    for index in range(rows):
        lon = round(118.75 + index * 0.01, 6)
        lat = round(32.03 + index * 0.01, 6)
        if index >= rows - bad_rows:
            record = ["", ""]
        elif extra_cols:
            record = [str(lon), str(lat), f"site-{index}", str(10 + index)]
        else:
            record = [str(lon), str(lat)]
        lines.append(",".join(record))
    return ("\n".join(lines) + "\n").encode("utf-8")


def declared_crs(crs: dict[str, Any]) -> dict[str, Any]:
    """A user-declared File CRS fact, as the Compiler requires for spatial files."""
    return {"schema": {"file": {"declared_crs": {"value": crs, "source": "user"}}}}
