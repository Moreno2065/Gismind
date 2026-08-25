"""Tests for YAML-driven ToolKit loading and ToolDisclosureController."""

from app.agents.toolkit.registry import (
    ALWAYS_VISIBLE_TOOLS,
    KERNEL_TOOLS,
    ToolDisclosureController,
    ToolKitRegistry,
)


# ---------------------------------------------------------------------------
# ToolKitRegistry
# ---------------------------------------------------------------------------


def test_registry_loads_data_io():
    """data_io toolkit should be loaded from YAML and marked default_active."""
    reg = ToolKitRegistry()
    tk = reg.get("data_io")
    assert tk is not None
    assert tk.default_active is True
    assert "geo_code" in tk.tools
    assert "query_poi" in tk.tools


def test_registry_loads_vector_analysis():
    """vector_analysis toolkit should be loaded from YAML."""
    reg = ToolKitRegistry()
    tk = reg.get("vector_analysis")
    assert tk is not None
    assert "buffer" in tk.tools
    assert "overlay" in tk.tools
    assert "voronoi" in tk.tools


def test_registry_loads_vector_overlay():
    """vector_overlay toolkit should be loaded from YAML."""
    reg = ToolKitRegistry()
    tk = reg.get("vector_overlay")
    assert tk is not None
    assert "overlay" in tk.tools
    assert "clip_layer" in tk.tools
    assert "extract_by_location" in tk.tools


def test_default_active():
    """default_active() should return names of toolkits with default_active=True."""
    reg = ToolKitRegistry()
    active = reg.default_active()
    assert "data_io" in active


def test_active_tools():
    """active_tools() should flatten tool names from given toolkit names."""
    reg = ToolKitRegistry()
    tools = reg.active_tools(["data_io"])
    assert "geo_code" in tools
    assert "buffer" not in tools  # buffer is in vector_analysis, not data_io


def test_active_tools_multiple():
    """active_tools() with multiple toolkits should merge and dedupe."""
    reg = ToolKitRegistry()
    tools = reg.active_tools(["vector_analysis", "vector_overlay"])
    assert "buffer" in tools
    assert "overlay" in tools
    assert "voronoi" in tools
    # overlay should appear only once
    assert tools.count("overlay") == 0 or tools.count("overlay") == 1


def test_get_unknown():
    """get() on an unknown name should return None."""
    reg = ToolKitRegistry()
    assert reg.get("nonexistent") is None


def test_names():
    """names() should return all registered toolkit names."""
    reg = ToolKitRegistry()
    names = reg.names()
    assert "data_io" in names
    assert "vector_analysis" in names
    assert "vector_overlay" in names


def test_to_catalog():
    """to_catalog() should return a dict suitable for prompt injection."""
    reg = ToolKitRegistry()
    catalog = reg.to_catalog()
    assert "data_io" in catalog
    assert "title" in catalog["data_io"]
    assert "tools" in catalog["data_io"]
    assert isinstance(catalog["data_io"]["tools"], list)


# ---------------------------------------------------------------------------
# ToolDisclosureController
# ---------------------------------------------------------------------------


def test_controller_default_visible_includes_kernel():
    """Default visible tools should include all KERNEL_TOOLS."""
    reg = ToolKitRegistry()
    controller = ToolDisclosureController(reg)
    # visible_tools with a sub-agent that has all tools
    all_tools = list(KERNEL_TOOLS) + ["geo_code", "buffer", "overlay"]
    visible = controller.visible_tools(all_tools)
    for kt in KERNEL_TOOLS:
        assert kt in visible


def test_controller_select_toolkits():
    """select_toolkits() should return info about what was selected."""
    controller = ToolDisclosureController()
    result = controller.select_toolkits({"toolkits": ["vector_analysis"]})
    assert "active_toolkits" in result
    assert "tools_added" in result


def test_controller_inspect_workspace():
    """inspect_workspace() should return a workspace summary."""
    controller = ToolDisclosureController()
    result = controller.inspect_workspace(
        {"session_vars": {"a": 1, "b": 2}, "workspace": None},
        {"query_type": "layers"},
    )
    assert "layers" in result
    assert "session_var_keys" in result
    assert "a" in result["session_var_keys"]
    assert "b" in result["session_var_keys"]


# ---------------------------------------------------------------------------
# ALWAYS_VISIBLE_TOOLS
# ---------------------------------------------------------------------------


def test_always_visible_contains_kernel():
    """ALWAYS_VISIBLE_TOOLS should contain all KERNEL_TOOLS."""
    for kt in KERNEL_TOOLS:
        assert kt in ALWAYS_VISIBLE_TOOLS


def test_always_visible_contains_data_io_tools():
    """ALWAYS_VISIBLE_TOOLS should contain tools from default_active toolkits."""
    assert "geo_code" in ALWAYS_VISIBLE_TOOLS
    assert "query_poi" in ALWAYS_VISIBLE_TOOLS
