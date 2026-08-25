"""Tool execution context — 独立模块避免循环 import.

Extracted from loop.py to break the circular dependency:
  loop.py → sandbox/tools.py → loop.py (old)
  loop.py → sandbox/tools.py → context.py (new)

See docs/02_data_models.md §4 for _ToolContext usage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class _ToolContext:
    """单个工具调用执行所需的上下文。

    字段：
        tool_call_id: 工具调用唯一 ID
        tool_name: 工具名称
        iteration: 当前迭代轮次
        params: 工具参数 dict
        results_data: 前序工具结果（dict[int, dict]）
        instances: 模块实例 dict（poi / geo_coder / analyzer / data_io / layer_builder）
        poi / geo_coder / analyzer / data_io / layer_builder: 从 instances 自动提取
    """

    tool_call_id: str
    tool_name: str
    iteration: int
    params: dict
    results_data: dict[int, Any] = field(default_factory=dict)
    instances: dict[str, Any] = field(default_factory=dict)

    # Convenience attributes — set in __post_init__ from instances dict
    poi: Any = None
    geo_coder: Any = None
    analyzer: Any = None
    data_io: Any = None
    layer_builder: Any = None
    raster_analyzer: Any = None

    def __post_init__(self) -> None:
        if not self.instances:
            return
        self.poi = self.instances.get("poi", self.poi)
        self.geo_coder = self.instances.get("geo_coder", self.geo_coder)
        self.analyzer = self.instances.get("analyzer", self.analyzer)
        self.data_io = self.instances.get("data_io", self.data_io)
        self.layer_builder = self.instances.get("layer_builder", self.layer_builder)
        self.raster_analyzer = self.instances.get("raster_analyzer", self.raster_analyzer)
