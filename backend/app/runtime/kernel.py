"""Planner/compiler-side Kernel semantics.

These operations shape planning context and are deliberately not Runtime
ToolContracts.  They are pure services so execution workers never dispatch
them as GIS tasks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class KernelService:
    """Small deterministic service used by Planner/Compiler callers."""

    def select_toolkit(self, toolkits: list[str] | None = None) -> dict[str, Any]:
        selected = sorted({str(item).strip() for item in (toolkits or []) if str(item).strip()})
        return {"selected": selected, "toolkits": selected}

    def inspect_workspace(self, path: str | None = None) -> dict[str, Any]:
        target = Path(path or ".").resolve()
        if not target.exists():
            return {"path": str(target), "exists": False, "entries": []}
        entries = sorted(item.name for item in target.iterdir())[:200] if target.is_dir() else []
        return {"path": str(target), "exists": True, "is_dir": target.is_dir(), "entries": entries}

    def suggest_skill(self, goal: str = "") -> dict[str, Any]:
        text = str(goal).lower()
        suggestions = ["gis-analysis"] if any(word in text for word in ("buffer", "空间", "地图", "raster", "矢量")) else []
        return {"suggestions": suggestions, "goal": goal}

    def load_skill(self, name: str) -> dict[str, Any]:
        name = str(name).strip()
        return {"name": name, "loaded": bool(name), "source": "planner"}

    def proactive_clarification(self, *, goal: str = "", missing: list[str] | None = None) -> dict[str, Any]:
        fields = sorted({str(item) for item in (missing or []) if str(item)})
        return {"needed": bool(fields), "missing": fields, "goal": goal}


kernel_service = KernelService()
