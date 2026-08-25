"""Tests for build_code_mode_prompt — including toolkit/skill injection (Task 4).

Verifies:
- Default prompt builds without toolkit/skill (backward compatible).
- toolkit_catalog adds "Available ToolKits" section.
- loaded_skills adds "Loaded Skills" section.
- Both can be supplied together.
- Invalid/empty inputs do not break the prompt.
"""
from __future__ import annotations

import pytest

from app.agents.planner_factory import (
    build_code_mode_prompt,
    inject_toolkit_and_skills,
)


# ---------------------------------------------------------------------------
# Backward-compatibility baseline
# ---------------------------------------------------------------------------


def test_prompt_baseline_no_toolkits_no_skills():
    """Without toolkit_catalog / loaded_skills, prompt is the base prompt."""
    prompt = build_code_mode_prompt("geo")
    assert isinstance(prompt, str)
    assert len(prompt) > 100
    # The base prompt should mention available functions
    assert "可用函数" in prompt or "function" in prompt.lower()
    # No ToolKits section in baseline
    assert "ToolKits" not in prompt
    assert "已加载技能" not in prompt


def test_prompt_baseline_all_known_roles():
    """All sub-agent roles should build a non-empty baseline prompt."""
    for role in ("geo", "poi", "geometer", "viz", "coder"):
        prompt = build_code_mode_prompt(role)
        assert isinstance(prompt, str)
        assert len(prompt) > 50


# ---------------------------------------------------------------------------
# toolkit_catalog injection
# ---------------------------------------------------------------------------


def test_prompt_with_toolkit_catalog_only():
    """toolkit_catalog should inject 'Available ToolKits' section (中文: 可用工具集)."""
    catalog = {
        "data_io": {
            "title": "Data IO",
            "description": "Load/export data",
            "tools": ["geo_code", "query_poi"],
        },
        "vector_analysis": {
            "title": "Vector Analysis",
            "description": "Buffer/overlay/voronoi",
            "tools": ["buffer", "overlay", "voronoi"],
        },
    }
    prompt = build_code_mode_prompt("geo", toolkit_catalog=catalog)
    assert "ToolKits" in prompt
    assert "data_io" in prompt
    assert "vector_analysis" in prompt
    assert "geo_code" in prompt
    # ToolKits section should reference select_toolkit hint
    assert "select_toolkit" in prompt


def test_prompt_with_empty_toolkit_catalog():
    """Empty dict is falsy in our impl — no section added (no crash)."""
    prompt = build_code_mode_prompt("geo", toolkit_catalog={})
    assert isinstance(prompt, str)
    # Empty dict is falsy → no section
    assert "ToolKits" not in prompt


def test_prompt_with_toolkit_catalog_uses_description():
    """Catalog description text appears in the prompt."""
    catalog = {
        "data_io": {
            "description": "TEST_DESCRIPTION_PHRASE_42",
            "tools": [],
        },
    }
    prompt = build_code_mode_prompt("geo", toolkit_catalog=catalog)
    assert "TEST_DESCRIPTION_PHRASE_42" in prompt


# ---------------------------------------------------------------------------
# loaded_skills injection
# ---------------------------------------------------------------------------


def test_prompt_with_loaded_skills_only():
    """loaded_skills should inject 'Loaded Skills' section (中文: 已加载技能)."""
    skills = {
        "meter_buffer": "Use projected CRS for meter-based buffers.",
        "spatial_join": "Check geometry type before spatial join.",
    }
    prompt = build_code_mode_prompt("geo", loaded_skills=skills)
    assert "已加载技能" in prompt
    assert "meter_buffer" in prompt
    assert "spatial_join" in prompt
    assert "projected CRS" in prompt


def test_prompt_with_empty_loaded_skills():
    """Empty dict is falsy in our impl — no section added (no crash)."""
    prompt = build_code_mode_prompt("geo", loaded_skills={})
    assert isinstance(prompt, str)
    assert "已加载技能" not in prompt


# ---------------------------------------------------------------------------
# Both supplied together
# ---------------------------------------------------------------------------


def test_prompt_with_toolkit_catalog_and_loaded_skills():
    """Both sections should be present when both supplied."""
    catalog = {
        "vector_analysis": {
            "description": "buffer+overlay toolkit",
            "tools": ["buffer", "overlay"],
        },
    }
    skills = {
        "meter_buffer": "米制缓冲最佳实践内容",
    }
    prompt = build_code_mode_prompt("geometer", toolkit_catalog=catalog, loaded_skills=skills)
    assert "ToolKits" in prompt
    assert "已加载技能" in prompt
    assert "vector_analysis" in prompt
    assert "米制缓冲最佳实践内容" in prompt


# ---------------------------------------------------------------------------
# inject_toolkit_and_skills helper (used internally)
# ---------------------------------------------------------------------------


def test_inject_toolkit_and_skills_no_injection_when_both_none():
    """Helper returns base_prompt unchanged when both args are None/empty."""
    base = "# Original prompt"
    assert inject_toolkit_and_skills(base) == base
    assert inject_toolkit_and_skills(base, None, None) == base
    assert inject_toolkit_and_skills(base, {}, {}) == base


def test_inject_toolkit_and_skills_appends_sections():
    """Helper appends sections when inputs are non-empty."""
    base = "# Original prompt"
    out = inject_toolkit_and_skills(
        base,
        toolkit_catalog={"x": {"description": "X toolkit", "tools": ["a"]}},
        loaded_skills={"y": "Y skill content"},
    )
    assert out.startswith(base)
    assert "x" in out
    assert "Y skill content" in out


def test_inject_toolkit_and_skills_with_string_value():
    """Toolkit catalog values that are non-dict strings are still handled."""
    base = "base"
    out = inject_toolkit_and_skills(
        base,
        toolkit_catalog={"tk1": "plain string description"},
    )
    assert "tk1" in out
    assert "plain string description" in out


# ---------------------------------------------------------------------------
# Negative cases
# ---------------------------------------------------------------------------


def test_prompt_with_unknown_role_raises():
    """Unknown role should raise KeyError (registry behavior)."""
    with pytest.raises(KeyError):
        build_code_mode_prompt("nonexistent_role_xyz")
