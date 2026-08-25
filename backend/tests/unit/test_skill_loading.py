"""Tests for YAML-frontmatter Markdown skill loading."""

from app.agents.skill.registry import SkillMeta, SkillRegistry


# ---------------------------------------------------------------------------
# SkillRegistry
# ---------------------------------------------------------------------------


def test_load_meter_buffer():
    """meter_buffer skill should be loaded from YAML-frontmatter MD."""
    reg = SkillRegistry()
    meta = reg.get("meter_buffer")
    assert meta is not None
    assert meta.name == "meter_buffer"
    assert "vector_analysis" in meta.requires_toolkits
    assert "input_crs" in meta.workspace_attention
    assert meta.max_chars == 500


def test_load_spatial_join():
    """spatial_join skill should be loaded from YAML-frontmatter MD."""
    reg = SkillRegistry()
    meta = reg.get("spatial_join")
    assert meta is not None
    assert meta.name == "spatial_join"
    assert "crs" in meta.workspace_attention
    assert meta.max_chars == 400


def test_read_meter_buffer_content():
    """read_content() should return the body of the skill (frontmatter stripped)."""
    reg = SkillRegistry()
    content = reg.read_content("meter_buffer")
    assert isinstance(content, str)
    assert len(content) > 0
    # Should contain the body, not the frontmatter
    assert "米制缓冲区" in content
    assert "EPSG:4326" in content
    # Frontmatter keys should NOT appear in body
    assert "requires_toolkits" not in content or content.count("requires_toolkits") == 0


def test_read_spatial_join_content():
    """read_content() for spatial_join should return trimmed body."""
    reg = SkillRegistry()
    content = reg.read_content("spatial_join")
    assert isinstance(content, str)
    assert len(content) > 0
    assert "空间连接" in content


def test_read_content_respects_max_chars():
    """read_content() should truncate to max_chars if set."""
    reg = SkillRegistry()
    meta = reg.get("spatial_join")
    assert meta is not None
    content = reg.read_content("spatial_join")
    assert len(content) <= meta.max_chars


def test_unknown_skill():
    """get() and read_content() should return None/empty for unknown skills."""
    reg = SkillRegistry()
    assert reg.get("nonexistent") is None
    assert reg.read_content("nonexistent") == ""


def test_names():
    """names() should return all registered skill names."""
    reg = SkillRegistry()
    names = reg.names()
    assert "meter_buffer" in names
    assert "spatial_join" in names


def test_to_catalog():
    """to_catalog() should return a dict with skill descriptions."""
    reg = SkillRegistry()
    catalog = reg.to_catalog()
    assert "meter_buffer" in catalog
    assert "description" in catalog["meter_buffer"]
    assert "requires_toolkits" in catalog["meter_buffer"]


def test_skill_meta_is_frozen():
    """SkillMeta should be a frozen (immutable) dataclass."""
    meta = SkillMeta(name="test", description="test")
    import dataclasses
    assert dataclasses.is_dataclass(meta)
    # Attempting to set an attribute should raise FrozenInstanceError
    import pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        meta.description = "changed"  # type: ignore
