"""LayerRecord dataclass — 工作区中的单个数据图层。

每条 LayerRecord 包含 layer_id、名称、类别、来源、血缘信息
以及元数据（CRS / geometry_type / feature_count / fields）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

LayerKind = Literal["vector", "raster", "table", "point", "polygon", "unknown"]


@dataclass
class LayerRecord:
    """工作区中的单个数据图层。

    Args:
        layer_id: 唯一图层标识（自动生成或外部指定）。
        name: 图层名称（通常是 LLM 代码中的变量名）。
        kind: 图层类别。
        source: 数据来源（文件路径 / GeoJSON dict / Redis key / "memory" / tool name）。
        parent_ids: 父图层 layer_id 列表（血缘追踪）。
        algorithm_id: 生成此图层的算法标识。
        parameters: 算法参数。
        metadata: 元数据字典，约定包含 crs / geometry_type / feature_count / fields。
    """

    layer_id: str
    name: str
    kind: LayerKind = "unknown"
    source: str | dict | None = None
    parent_ids: list[str] = field(default_factory=list)
    algorithm_id: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON-safe dict。"""
        return {
            "layer_id": self.layer_id,
            "name": self.name,
            "kind": self.kind,
            "source": self.source if isinstance(self.source, (str, type(None))) else "<dict>",
            "parent_ids": list(self.parent_ids),
            "algorithm_id": self.algorithm_id,
            "parameters": dict(self.parameters),
            "metadata": dict(self.metadata),
        }
