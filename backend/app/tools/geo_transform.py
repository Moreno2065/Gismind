"""坐标转换工具集。

实现 WGS84 / GCJ02 / BD09 三套坐标系之间的数学偏转转换，以及
坐标系自动识别、国内范围判断、haversine 距离计算等辅助函数。

核心原则（参考 GIS_Agent_技术文档.md §4.2）：
1. GCJ02 没有标准 EPSG 编码，不能用 pyproj 做偏转，必须用数学算法
2. 国外坐标不偏转（out_of_china 判断），高德海外底图自动切换为 WGS84
3. 所有 pyproj 投影计算应在 WGS84/CGCS2000 上进行，不能直接用 GCJ02

算法来源：标准公开 GCJ02 火星坐标偏转算法（coordTransform_py）。
"""

import math

# GCJ02 偏转算法常量
_A = 6378245.0  # 椭球长半轴
_EE = 0.00669342162296594323  # 偏心率平方

# BD09 转换常量
_X_PI = math.pi * 3000.0 / 180.0

# 中国大陆范围边界（用于 out_of_china / is_china_bbox 判断）
CHINA_BBOX: tuple[float, float, float, float] = (73.66, 3.86, 135.05, 53.55)

# --- 内部别名，用于向后兼容 ---
_CHINA_LNG_MIN = CHINA_BBOX[0]
_CHINA_LNG_MAX = CHINA_BBOX[2]
_CHINA_LAT_MIN = CHINA_BBOX[1]
_CHINA_LAT_MAX = CHINA_BBOX[3]


def _transform_lat(lng: float, lat: float) -> float:
    """GCJ02 纬度偏转辅助函数。"""
    ret = (
        -100.0
        + 2.0 * lng
        + 3.0 * lat
        + 0.2 * lat * lat
        + 0.1 * lng * lat
        + 0.2 * math.sqrt(abs(lng))
    )
    ret += (
        (20.0 * math.sin(6.0 * lng * math.pi) + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0 / 3.0
    )
    ret += (
        (20.0 * math.sin(lat * math.pi) + 40.0 * math.sin(lat / 3.0 * math.pi)) * 2.0 / 3.0
    )
    ret += (
        (160.0 * math.sin(lat / 12.0 * math.pi) + 320 * math.sin(lat * math.pi / 30.0)) * 2.0 / 3.0
    )
    return ret


def _transform_lng(lng: float, lat: float) -> float:
    """GCJ02 经度偏转辅助函数。"""
    ret = (
        300.0
        + lng
        + 2.0 * lat
        + 0.1 * lng * lng
        + 0.1 * lng * lat
        + 0.1 * math.sqrt(abs(lng))
    )
    ret += (
        (20.0 * math.sin(6.0 * lng * math.pi) + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0 / 3.0
    )
    ret += (
        (20.0 * math.sin(lng * math.pi) + 40.0 * math.sin(lng / 3.0 * math.pi)) * 2.0 / 3.0
    )
    ret += (
        (150.0 * math.sin(lng / 12.0 * math.pi) + 300.0 * math.sin(lng / 30.0 * math.pi)) * 2.0 / 3.0
    )
    return ret


def out_of_china(lng: float, lat: float) -> bool:
    """判断坐标是否在中国大陆范围外。

    国内范围：经度 73.66-135.05，纬度 3.86-53.55。
    国外坐标不做 GCJ02 偏转（高德海外底图自动切换为无偏移 WGS84）。
    """
    if lng < _CHINA_LNG_MIN or lng > _CHINA_LNG_MAX:
        return True
    if lat < _CHINA_LAT_MIN or lat > _CHINA_LAT_MAX:
        return True
    return False


def wgs84_to_gcj02(lng: float, lat: float) -> tuple[float, float]:
    """WGS84 -> GCJ02 火星坐标偏转。

    国外坐标直接返回原值（不偏转）。
    """
    if out_of_china(lng, lat):
        return (lng, lat)

    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    mglat = lat + dlat
    mglng = lng + dlng
    return (mglng, mglat)


def gcj02_to_wgs84(lng: float, lat: float) -> tuple[float, float]:
    """GCJ02 -> WGS84 逆偏转。

    采用迭代法（牛顿迭代逼近）：从 gcj02 坐标出发，反复用
    wgs84_to_gcj02 正向偏转校验，逐步逼近真实 wgs84 坐标。
    迭代 30 次可使往返误差降至亚毫米级，远优于 < 1m 要求。

    国外坐标直接返回原值。
    """
    if out_of_china(lng, lat):
        return (lng, lat)

    # 迭代初值：用直接法（减偏移）给一个粗略 wgs84
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    wgs_lng = lng - dlng
    wgs_lat = lat - dlat

    # 迭代精化：目标是使 wgs84_to_gcj02(wgs) == gcj02
    for _ in range(30):
        gcj_lng, gcj_lat = wgs84_to_gcj02(wgs_lng, wgs_lat)
        # 残差
        err_lng = gcj_lng - lng
        err_lat = gcj_lat - lat
        wgs_lng -= err_lng
        wgs_lat -= err_lat
        # 收敛判定（0.0000001 度约 1cm）
        if abs(err_lng) < 1e-8 and abs(err_lat) < 1e-8:
            break

    return (wgs_lng, wgs_lat)


def gcj02_to_bd09(lng: float, lat: float) -> tuple[float, float]:
    """GCJ02 -> BD09 百度坐标转换。"""
    z = math.sqrt(lng * lng + lat * lat) + 0.00002 * math.sin(lat * _X_PI)
    theta = math.atan2(lat, lng) + 0.000003 * math.cos(lng * _X_PI)
    bd_lng = z * math.cos(theta) + 0.0065
    bd_lat = z * math.sin(theta) + 0.006
    return (bd_lng, bd_lat)


def bd09_to_gcj02(lng: float, lat: float) -> tuple[float, float]:
    """BD09 -> GCJ02 百度坐标逆转换。"""
    x = lng - 0.0065
    y = lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * _X_PI)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * _X_PI)
    gcj_lng = z * math.cos(theta)
    gcj_lat = z * math.sin(theta)
    return (gcj_lng, gcj_lat)


def wgs84_to_bd09(lng: float, lat: float) -> tuple[float, float]:
    """WGS84 -> BD09 组合转换（WGS84 -> GCJ02 -> BD09）。"""
    gcj_lng, gcj_lat = wgs84_to_gcj02(lng, lat)
    return gcj02_to_bd09(gcj_lng, gcj_lat)


def bd09_to_wgs84(lng: float, lat: float) -> tuple[float, float]:
    """BD09 -> WGS84 组合转换（BD09 -> GCJ02 -> WGS84）。"""
    gcj_lng, gcj_lat = bd09_to_gcj02(lng, lat)
    return gcj02_to_wgs84(gcj_lng, gcj_lat)


def is_china_bbox(bbox: tuple) -> bool:
    """判断 bbox 是否在中国大陆范围内。

    Args:
        bbox: (minLng, minLat, maxLng, maxLat)

    Returns:
        True 如果 bbox 完全在中国大陆经纬度范围内。
    """
    min_lng, min_lat, max_lng, max_lat = bbox
    bbox_min_lng, bbox_min_lat, bbox_max_lng, bbox_max_lat = CHINA_BBOX
    return (
        min_lng >= bbox_min_lng
        and max_lng <= bbox_max_lng
        and min_lat >= bbox_min_lat
        and max_lat <= bbox_max_lat
    )


def auto_detect_crs(bbox: tuple | None = None, file_path: str | None = None) -> str:
    """启发式识别坐标系。

    优先级：
    1. 若有 file_path 且含 .prj 文件，读取声明（本函数暂不解析文件，留给 DataIO）
    2. 根据 bbox 坐标范围判断：
       - 经度在 [-180, 180]、纬度在 [-90, 90]：地理坐标系 WGS84 (EPSG:4326)
       - 数值远大于 180（如百万级）：高斯投影，根据横坐标反算中央经线对应 EPSG

    Args:
        bbox: (minLng, minLat, maxLng, maxLat)，可选
        file_path: 文件路径，可选（预留，本实现不解析）

    Returns:
        坐标系标识字符串，如 "EPSG:4326"、"EPSG:4548"、"EPSG:4549"
    """
    if bbox is None:
        # 无 bbox 信息时默认 WGS84
        return "EPSG:4326"

    min_x, min_y, max_x, max_y = bbox

    # 高斯投影坐标：数值远大于 180（通常是 6 位或 8 位数字，单位米）
    # CGCS2000 / Gauss-Kruger 3度带，横坐标带号前缀
    if abs(min_x) > 180 or abs(max_x) > 180:
        # 根据横坐标前缀反推 3 度带带号，再映射到 EPSG。
        # CGCS2000 3 度带 zone 25 起对应 EPSG:4513，zone = int(x / 1_000_000)。
        x_val = max(abs(min_x), abs(max_x))
        zone = int(x_val / 1_000_000)
        if zone < 25 or zone > 49:
            raise ValueError(
                f"无法识别的 CGCS2000 3度带带号：{zone}（横坐标 {x_val}），"
                f"仅支持 25-49 带"
            )
        epsg_code = 4513 + zone - 25
        return f"EPSG:{epsg_code}"

    # 地理坐标系范围：经纬度
    if (
        -180.0 <= min_x <= 180.0
        and -180.0 <= max_x <= 180.0
        and -90.0 <= min_y <= 90.0
        and -90.0 <= max_y <= 90.0
    ):
        # 国内地理坐标默认 WGS84（GCJ02 无标准 EPSG，由数据源标注）
        # 国外也是 WGS84
        return "EPSG:4326"

    # 兜底
    return "EPSG:4326"


def haversine_m(p1: tuple, p2: tuple) -> float:
    """计算两点间球面距离（haversine 公式），单位米。

    Args:
        p1: (lng, lat)
        p2: (lng, lat)

    Returns:
        距离（米）
    """
    lng1, lat1 = p1
    lng2, lat2 = p2
    earth_radius = 6371000.0  # 地球平均半径，米

    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)

    a = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlng / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return earth_radius * c
