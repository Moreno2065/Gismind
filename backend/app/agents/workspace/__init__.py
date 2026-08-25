"""Workspace 模块：LayerRecord / WorkspaceState / 别名解析。

公开导出：
- WorkspaceState
- LayerRecord
"""

from __future__ import annotations

from app.agents.workspace.layer_record import LayerRecord
from app.agents.workspace.state import WorkspaceState
from app.agents.workspace.state import _is_geo_like, _infer_kind, _infer_metadata

__all__ = [
    "LayerRecord",
    "WorkspaceState",
    "_is_geo_like",
    "_infer_kind",
    "_infer_metadata",
]
