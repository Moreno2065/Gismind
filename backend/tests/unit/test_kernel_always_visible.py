"""Tests that KERNEL tools are registered in TOOL_SPECS and always visible."""

from app.agents.registry import TOOL_SPECS
from app.agents.toolkit.registry import ALWAYS_VISIBLE_TOOLS, KERNEL_TOOLS


# ---------------------------------------------------------------------------
# TOOL_SPECS registration
# ---------------------------------------------------------------------------


def test_all_kernel_tools_registered():
    """Each KERNEL tool should have a corresponding ToolSpec in TOOL_SPECS."""
    for tool_name in KERNEL_TOOLS:
        assert tool_name in TOOL_SPECS, f"{tool_name} missing from TOOL_SPECS"


def test_kernel_tools_have_descriptions():
    """Each KERNEL tool should have a non-empty description."""
    for tool_name in KERNEL_TOOLS:
        spec = TOOL_SPECS[tool_name]
        assert spec.description, f"{tool_name} has empty description"
        assert len(spec.description) > 10, f"{tool_name} description too short"


def test_kernel_tools_are_inline():
    """All KERNEL tools should use inline executor (no IO dependencies)."""
    for tool_name in KERNEL_TOOLS:
        spec = TOOL_SPECS[tool_name]
        assert spec.executor_type == "inline", (
            f"{tool_name} should be inline, got {spec.executor_type}"
        )


def test_kernel_tools_not_deprecated():
    """No KERNEL tool should be marked as deprecated."""
    for tool_name in KERNEL_TOOLS:
        spec = TOOL_SPECS[tool_name]
        assert not spec.deprecated, f"{tool_name} should not be deprecated"


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


def test_kernel_tools_in_always_visible():
    """Every KERNEL tool must appear in ALWAYS_VISIBLE_TOOLS."""
    for tool_name in KERNEL_TOOLS:
        assert tool_name in ALWAYS_VISIBLE_TOOLS, (
            f"{tool_name} not in ALWAYS_VISIBLE_TOOLS"
        )


def test_always_visible_contains_exactly():
    """ALWAYS_VISIBLE_TOOLS should contain KERNEL_TOOLS plus default-active tools.
    
    ALWAYS_VISIBLE_TOOLS is lazily rebuilt by _rebuild_always_visible when a
    ToolDisclosureController is constructed.  Tests must trigger it manually.
    """
    from app.agents.toolkit.registry import ToolKitRegistry, _rebuild_always_visible
    _rebuild_always_visible(ToolKitRegistry())

    for kt in KERNEL_TOOLS:
        assert kt in ALWAYS_VISIBLE_TOOLS, f"{kt} not in ALWAYS_VISIBLE_TOOLS"
    assert len(ALWAYS_VISIBLE_TOOLS) > len(KERNEL_TOOLS), (
        f"ALWAYS_VISIBLE_TOOLS only has kernel tools: {ALWAYS_VISIBLE_TOOLS}"
    )


# ---------------------------------------------------------------------------
# Exact tool names match spec
# ---------------------------------------------------------------------------


def test_kernel_tool_names_match_spec():
    """KERNEL_TOOLS tuple should match the 5 tools specified in the design doc."""
    expected = (
        "select_toolkit",
        "inspect_workspace",
        "suggest_skill",
        "load_skill",
        "proactive_clarification",
    )
    assert KERNEL_TOOLS == expected
