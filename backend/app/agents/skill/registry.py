"""SkillRegistry — YAML-frontmatter Markdown skill loader.

Each skill is a ``.md`` file under ``backend/resources/skills/`` whose first
non-empty lines are YAML frontmatter delimited by ``---``:

.. code-block:: markdown

    ---
    name: meter_buffer
    description: 米制缓冲区最佳实践
    requires_toolkits: [vector_analysis]
    workspace_attention: [input_crs, geometry_type]
    risk_awareness: [geographic_crs_metric_buffer]
    strategy_guidance:
      - "米制缓冲前必须先确认输入图层 CRS"
      - "如 CRS 是 EPSG:4326，先 reproject 到本地 UTM 带"
    max_chars: 500
    ---

    # 米制缓冲区
    ...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillMeta:
    """Metadata for a single GIS best-practice skill."""

    name: str
    description: str
    requires_toolkits: tuple[str, ...] = ()
    workspace_attention: tuple[str, ...] = ()
    risk_awareness: tuple[str, ...] = ()
    strategy_guidance: tuple[str, ...] = ()
    max_chars: int = 0
    path: str = ""


class SkillRegistry:
    """Scans ``backend/resources/skills/`` for ``*.md`` files with YAML frontmatter.

    Usage::

        reg = SkillRegistry()
        meta = reg.get("meter_buffer")
        content = reg.read_content("meter_buffer")
    """

    def __init__(self, skills_root: str | Path | None = None) -> None:
        self._root = (
            Path(skills_root)
            if skills_root
            else Path(__file__).resolve().parents[3] / "resources" / "skills"
        )
        self._skills: dict[str, SkillMeta] = {}
        self._refresh()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        """Re-scan the skills directory."""
        self._skills.clear()
        if not self._root.is_dir():
            logger.warning("SkillRegistry root not found: %s", self._root)
            return
        for path in sorted(self._root.glob("*.md")):
            meta = self._parse(path)
            if meta is not None:
                self._skills[meta.name] = meta

    def _parse(self, path: Path) -> SkillMeta | None:
        """Parse a single skill ``.md`` file.

        Returns ``None`` when the file does not start with ``---`` (no
        frontmatter) or when YAML parsing fails.
        """
        text = path.read_text(encoding="utf-8")
        stripped = text.lstrip()
        if not stripped.startswith("---"):
            return None

        # Find the closing ---
        end = stripped.find("---", 3)
        if end == -1:
            return None

        frontmatter = stripped[3:end]
        try:
            data: dict[str, Any] = yaml.safe_load(frontmatter) or {}
        except yaml.YAMLError as exc:
            logger.warning("YAML parse error in %s: %s", path.name, exc)
            return None

        name: str = data.get("name") or path.stem
        return SkillMeta(
            name=name,
            description=data.get("description", ""),
            requires_toolkits=tuple(data.get("requires_toolkits") or ()),
            workspace_attention=tuple(data.get("workspace_attention") or ()),
            risk_awareness=tuple(data.get("risk_awareness") or ()),
            strategy_guidance=tuple(data.get("strategy_guidance") or ()),
            max_chars=int(data.get("max_chars") or 0),
            path=str(path),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, name: str) -> SkillMeta | None:
        """Return the ``SkillMeta`` for *name*, or ``None``."""
        return self._skills.get(name)

    def names(self) -> tuple[str, ...]:
        """Return all registered skill names."""
        return tuple(self._skills.keys())

    def read_content(self, name: str) -> str:
        """Return the body content of skill *name* (frontmatter stripped).

        Returns an empty string when the skill is unknown.
        """
        meta = self.get(name)
        if meta is None:
            return ""
        if not meta.path:
            return ""
        text = Path(meta.path).read_text(encoding="utf-8")
        stripped = text.lstrip()
        if stripped.startswith("---"):
            end = stripped.find("---", 3)
            if end != -1:
                text = stripped[end + 3:].strip()
        if meta.max_chars and len(text) > meta.max_chars:
            text = text[:meta.max_chars]
        return text

    def to_catalog(self) -> dict[str, dict[str, Any]]:
        """Return a plain-dict catalog suitable for prompt injection."""
        catalog: dict[str, dict[str, Any]] = {}
        for name, meta in self._skills.items():
            catalog[name] = {
                "description": meta.description,
                "requires_toolkits": list(meta.requires_toolkits),
                "risk_awareness": list(meta.risk_awareness),
            }
        return catalog
