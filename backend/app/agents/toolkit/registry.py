"""ToolKitRegistry + ToolDisclosureController + KERNEL_TOOLS + ALWAYS_VISIBLE_TOOLS.

KERNEL_TOOLS are the 5 semantic tools always visible to every sub-agent:
  select_toolkit, inspect_workspace, suggest_skill, load_skill, proactive_clarification.
ALWAYS_VISIBLE_TOOLS are the concatenation of KERNEL_TOOLS plus any tool names
that appear in a default_active Toolkit.

ToolDisclosureController manages which ToolKits are currently active and computes
the intersection of visible tool names for a sub-agent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KERNEL_TOOLS: tuple[str, ...] = (
    "select_toolkit",
    "inspect_workspace",
    "suggest_skill",
    "load_skill",
    "proactive_clarification",
)

# Built after YAML loading: KERNEL_TOOLS + tools from default_active toolkits.
# Populated by ToolDisclosureController._build_always_visible().
ALWAYS_VISIBLE_TOOLS: list[str] = list(KERNEL_TOOLS)


# ---------------------------------------------------------------------------
# ToolKitDefinition
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolKitDefinition:
    """Immutable definition for a single Toolkit read from a YAML resource file."""

    name: str
    title: str
    description: str
    tools: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    default_active: bool = False


# ---------------------------------------------------------------------------
# ToolKitRegistry
# ---------------------------------------------------------------------------

class ToolKitRegistry:
    """Loads ToolKit definitions from ``backend/resources/toolkits/*.yaml``.

    Falls back to a built-in default set when the YAML directory does not exist
    (e.g. during early development or testing without resource files).
    """

    def __init__(self, toolkits: dict[str, ToolKitDefinition] | None = None) -> None:
        self._toolkits: dict[str, ToolKitDefinition] = {}
        if toolkits is not None:
            self._toolkits.update(toolkits)
            return
        loaded = self._load_from_yaml()
        if loaded:
            self._toolkits.update(loaded)
        else:
            self._toolkits.update(self._defaults())

    # ------------------------------------------------------------------
    # YAML loading
    # ------------------------------------------------------------------

    def _load_from_yaml(self) -> dict[str, ToolKitDefinition] | None:
        """Scan ``backend/resources/toolkits/`` for ``*.yaml`` files.

        Returns ``None`` when the directory is missing (the caller falls back
        to built-in defaults).
        """
        root = Path(__file__).resolve().parents[3] / "resources" / "toolkits"
        if not root.is_dir():
            return None
        toolkits: dict[str, ToolKitDefinition] = {}
        for path in sorted(root.glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            name: str = data.get("name") or path.stem
            toolkits[name] = ToolKitDefinition(
                name=name,
                title=data.get("title", name),
                description=data.get("description", ""),
                tools=tuple(data.get("tools", ())),
                default_active=bool(data.get("default_active", False)),
            )
        return toolkits if toolkits else None

    # ------------------------------------------------------------------
    # Built-in defaults  (used when YAML directory is absent)
    # ------------------------------------------------------------------

    @staticmethod
    def _defaults() -> dict[str, ToolKitDefinition]:
        return {
            "data_io": ToolKitDefinition(
                name="data_io",
                title="Data IO",
                description="Load/export data",
                tools=("geo_code", "query_poi", "data_io_read", "map_layer_build"),
                default_active=True,
            ),
            "vector_analysis": ToolKitDefinition(
                name="vector_analysis",
                title="Vector analysis",
                description="Buffer, overlay, voronoi, isochrone, dissolve, merge, spatial join",
                tools=("buffer", "overlay", "voronoi", "isochrone", "dissolve_layer", "merge_layers", "join_by_location", "join_by_nearest", "count_points_in_polygon"),
            ),
            "vector_overlay": ToolKitDefinition(
                name="vector_overlay",
                title="Vector overlay",
                description="Overlay, clip, spatial filtering",
                tools=("overlay", "clip_layer", "extract_by_location"),
            ),
            "vector_transform": ToolKitDefinition(
                name="vector_transform",
                title="Vector transform",
                description="Reprojection, centroid, simplification, geometry validation",
                tools=("reproject_layer", "centroid_layer", "simplify_geometry", "fix_geometries", "check_validity", "multipart_to_singlepart"),
            ),
            "raster": ToolKitDefinition(
                name="raster",
                title="Raster analysis",
                description="Raster reprojection, clipping, calculator, zonal statistics, terrain analysis",
                tools=("reproject_raster", "clip_raster_by_mask", "clip_raster_by_extent", "raster_calculator", "zonal_statistics", "raster_sampling", "rasterize_vector", "polygonize_raster", "slope", "aspect", "hillshade", "contour", "reclassify_raster", "terrain_ruggedness_index", "topographic_position_index", "roughness"),
            ),
            "attribute_data": ToolKitDefinition(
                name="attribute_data",
                title="Attribute data",
                description="Attribute filtering, field management, field calculation",
                tools=("extract_by_attribute", "keep_fields", "rename_field", "field_calculator"),
            ),
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, name: str) -> ToolKitDefinition | None:
        """Return the definition for *name*, or ``None``."""
        return self._toolkits.get(name)

    def active_tools(self, active_names: list[str]) -> tuple[str, ...]:
        """Flatten tool names from every toolkit in *active_names* (deduplicated)."""
        seen: list[str] = []
        for tk_name in active_names:
            tk = self.get(tk_name)
            if tk:
                for t in tk.tools:
                    if t not in seen:
                        seen.append(t)
        return tuple(seen)

    def default_active(self) -> list[str]:
        """Return names of toolkits whose ``default_active`` flag is ``True``."""
        return [name for name, tk in self._toolkits.items() if tk.default_active]

    def names(self) -> tuple[str, ...]:
        """Return all registered toolkit names."""
        return tuple(self._toolkits.keys())

    def to_catalog(self) -> dict[str, dict[str, Any]]:
        """Return a plain-dict catalog suitable for prompt injection."""
        catalog: dict[str, dict[str, Any]] = {}
        for name, tk in self._toolkits.items():
            catalog[name] = {
                "title": tk.title,
                "description": tk.description,
                "tools": list(tk.tools),
                "default_active": tk.default_active,
            }
        return catalog


# ---------------------------------------------------------------------------
# ToolDisclosureController
# ---------------------------------------------------------------------------

class ToolDisclosureController:
    """Manages active toolkits and computes the visible tool set for a sub-agent.

    The visible tool set is the **union** of:
    1. ``ALWAYS_VISIBLE_TOOLS`` (always present)
    2. Tools from currently selected toolkits (via ``select_toolkits()``)
    """

    def __init__(self, registry: ToolKitRegistry | None = None) -> None:
        self._registry = registry or ToolKitRegistry()
        # Start with default-active toolkits
        self._active_toolkits: list[str] = list(self._registry.default_active())
        _rebuild_always_visible(self._registry)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    @property
    def active_toolkits(self) -> list[str]:
        """Return the list of currently active toolkit names."""
        return list(self._active_toolkits)

    @property
    def registry(self) -> ToolKitRegistry:
        return self._registry

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def select_toolkits(self, params: dict[str, Any]) -> dict[str, Any]:
        """Activate named toolkits.

        Accepts ``params`` with key ``"toolkits"`` (list of toolkit names).
        Unknown names are silently ignored.

        Returns a summary dict::

            {"active_toolkits": [...], "tools_added": [...]}
        """
        toolkits: list[str] = params.get("toolkits") or []
        before = set()
        for name in self._active_toolkits:
            tk = self._registry.get(name)
            if tk:
                before.update(tk.tools)

        self._active_toolkits = list(dict.fromkeys(toolkits))  # dedupe, preserve order

        after = set()
        for name in self._active_toolkits:
            tk = self._registry.get(name)
            if tk:
                after.update(tk.tools)

        added = sorted(after - before)
        return {"active_toolkits": list(self._active_toolkits), "tools_added": added}

    def visible_tools(self, sub_agent_tool_names: list[str]) -> list[str]:
        """Return the intersection of *sub_agent_tool_names* with the active set.

        The active set is: ALWAYS_VISIBLE_TOOLS + tools from active toolkits.
        """
        active_set = set(ALWAYS_VISIBLE_TOOLS)
        for tk_name in self._active_toolkits:
            tk = self._registry.get(tk_name)
            if tk:
                active_set.update(tk.tools)

        result = [n for n in sub_agent_tool_names if n in active_set]
        return result

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def inspect_workspace(
        self,
        state: dict[str, Any],
        params: dict[str, Any],
        registry: ToolKitRegistry | None = None,
    ) -> dict[str, Any]:
        """Return a human-readable summary of the current workspace.

        *state* should contain ``"session_vars"`` (dict) and optionally
        ``"workspace"`` (a WorkspaceState instance).

        Returns a dict with keys ``layers``, ``fields``, ``active_toolkits``.
        """
        _reg = registry or self._registry
        session_vars = state.get("session_vars") or {}
        workspace = state.get("workspace")

        layers_info: list[dict[str, Any]] = []
        if workspace is not None and hasattr(workspace, "to_dict"):
            ws_dict = workspace.to_dict()
            layers_info = ws_dict.get("layers", [])

        fields_summary: list[str] = []
        for rec in layers_info:
            meta = rec.get("metadata") or {}
            fields = meta.get("fields") or []
            fields_summary.append(f"{rec.get('name', '?')}: {list(fields)}")

        return {
            "layers": layers_info,
            "fields": fields_summary,
            "active_toolkits": list(self._active_toolkits),
            "session_var_keys": list(session_vars.keys()),
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _rebuild_always_visible(registry: ToolKitRegistry) -> None:
    """Recompute ``ALWAYS_VISIBLE_TOOLS`` = KERNEL_TOOLS + default_active tools."""
    seen: dict[str, int] = {}
    for t in KERNEL_TOOLS:
        seen[t] = 1
    for name in registry.default_active():
        tk = registry.get(name)
        if tk:
            for t in tk.tools:
                seen.setdefault(t, 1)
    ALWAYS_VISIBLE_TOOLS[:] = list(seen.keys())
