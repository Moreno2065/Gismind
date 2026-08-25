"""WorkspaceState — session_vars 的无侵入包装。

把 session_vars 扁 dict 提升为带图层记录与别名解析的工作区管理器。
保留原写入语义（session_vars.update），Google JSON 类数据时额外建 LayerRecord。

公开 API：
- WorkspaceState(session_vars)          共享引用包装
- .add_layer(...) → LayerRecord         注册图层
- .resolve(ref) → LayerRecord           别名解析
- .has_layer(ref) → bool                检查存在
- .latest_layer() → LayerRecord | None  最新图层
- .to_dict() → dict                      序列化
- .from_dict(sv, payload) → WS          反序列化重建
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.agents.workspace.layer_record import LayerKind, LayerRecord
from app.agents.workspace.resolver import resolve_ref


class WorkspaceState:
    """无侵入包装 session_vars；保留原写入语义，仅 GeoJSON-类数据时额外建 LayerRecord。"""

    def __init__(self, session_vars: dict[str, Any]):
        # 共享引用，不深拷贝（from_dict 通过 _sv 引用回写时使用）
        self._sv: dict[str, Any] = session_vars
        self._layers: dict[str, LayerRecord] = {}
        self._aliases: dict[str, str] = {}

    # ---- 写入 ----

    def add_layer(
        self,
        *,
        name: str,
        kind: LayerKind = "unknown",
        source: str | dict | None = None,
        parent_ids: list[str] | None = None,
        algorithm_id: str = "",
        parameters: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        layer_id: str | None = None,
    ) -> LayerRecord:
        """注册一个新图层到工作区。

        Args:
            name: 图层名称（通常即 LLM 代码中的变量名）。
            kind: 图层类别。
            source: 来源说明。
            parent_ids: 父图层 ID 列表。
            algorithm_id: 生成此图层的算法标识。
            parameters: 算法参数。
            metadata: 元数据字典。
            layer_id: 可选，不传则自动生成 ``{name}_{uuid_hex8}``。

        Returns:
            新创建的 LayerRecord。
        """
        lid = layer_id or f"{name}_{uuid4().hex[:8]}"
        record = LayerRecord(
            layer_id=lid,
            name=name,
            kind=kind,
            source=source,
            parent_ids=list(parent_ids or []),
            algorithm_id=algorithm_id,
            parameters=dict(parameters or {}),
            metadata=dict(metadata or {}),
        )
        self._layers[lid] = record
        self._aliases[name] = lid
        self._aliases[name.lower()] = lid
        return record

    # ---- 读取 ----

    def resolve(self, ref: str) -> LayerRecord:
        """通过别名 / layer_id / 特殊引用解析图层。

        Raises:
            KeyError: 引用未找到。
        """
        lid = resolve_ref(ref, self._layers, self._aliases)
        if lid is None or lid not in self._layers:
            # 尝试直接用 layer_id 匹配
            if ref in self._layers:
                return self._layers[ref]
            raise KeyError(f"Unknown layer reference: {ref}")
        return self._layers[lid]

    def has_layer(self, ref: str) -> bool:
        """判断引用是否对应一个已注册图层。"""
        try:
            self.resolve(ref)
            return True
        except KeyError:
            return False

    def latest_layer(self) -> LayerRecord | None:
        """返回最后添加的图层，若无则返回 None。"""
        if not self._layers:
            return None
        return list(self._layers.values())[-1]

    # ---- 序列化 ----

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON-safe dict。"""
        return {
            "layers": [r.to_dict() for r in self._layers.values()],
            "aliases": dict(self._aliases),
        }

    @classmethod
    def from_dict(cls, sv: dict[str, Any], payload: dict[str, Any]) -> WorkspaceState:
        """从 to_dict 的输出重建 WorkspaceState。

        Args:
            sv: session_vars 共享引用（from_dict 写入的图层会通过 _sv 回写）。
            payload: to_dict() 输出的 dict，含 {"layers": [...], "aliases": {...}}。
        """
        ws = cls(sv)
        for item in payload.get("layers") or []:
            if not isinstance(item, dict):
                continue
            ws.add_layer(
                name=item.get("name", ""),
                kind=item.get("kind", "unknown"),
                source=item.get("source"),
                parent_ids=item.get("parent_ids", []),
                algorithm_id=item.get("algorithm_id", ""),
                parameters=item.get("parameters", {}),
                metadata=item.get("metadata", {}),
                layer_id=item.get("layer_id"),
            )
        return ws


# ============================================================
# Geo-helper 函数（供 executor.py 注入点使用）
# ============================================================


def _is_geo_like(v: Any) -> bool:
    """判断值是否含 GIS geometry 数据。"""
    if hasattr(v, "geometry"):  # GeoDataFrame / GeoSeries
        return True
    if isinstance(v, dict):
        if v.get("type") == "FeatureCollection" or "features" in v:
            return True
        if "coordinates" in v:
            return True
    if type(v).__module__ == "shapely.geometry":
        return True
    return False


def _first_geometry_type(v: dict) -> str:
    """从 GeoJSON dict 中提取第一个 geometry 的 type。"""
    feats = v.get("features") or v.get("geometries") or []
    for f in feats:
        geom = f.get("geometry") if isinstance(f, dict) else None
        if geom:
            return str(geom.get("type", ""))
    return ""


def _infer_kind(v: Any) -> LayerKind:
    """推断 layer kind。"""
    if hasattr(v, "geometry") and hasattr(v, "dtypes"):
        return "vector"  # GeoDataFrame
    if isinstance(v, dict):
        gtype = _first_geometry_type(v)
        return {"Point": "point", "Polygon": "polygon"}.get(gtype, "vector")
    if type(v).__module__ == "shapely.geometry":
        return {"Point": "point", "Polygon": "polygon"}.get(type(v).__name__, "vector")
    return "unknown"


def _infer_metadata(v: Any) -> dict[str, Any]:
    """从值中提取 crs / geometry_type / feature_count / fields。"""
    meta: dict[str, Any] = {}
    if hasattr(v, "crs") and v.crs:
        meta["crs"] = str(v.crs)
    if hasattr(v, "__len__"):
        meta["feature_count"] = len(v)
    if isinstance(v, dict):
        feats = v.get("features") or []
        meta["feature_count"] = len(feats)
        if feats:
            meta["geometry_type"] = feats[0].get("geometry", {}).get("type", "")
    if hasattr(v, "columns"):
        meta["fields"] = list(v.columns)
    return meta
