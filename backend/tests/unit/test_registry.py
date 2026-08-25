from app.agents.registry import REGISTRY, get_spec, list_roles


def test_registry_has_six_roles():
    roles = set(list_roles())
    assert roles == {"geo", "poi", "geometer", "viz", "coder", "verifier"}


def test_poi_has_query_tool():
    spec = get_spec("poi")
    assert "query_poi" in spec.tool_names


def test_coder_only_uses_code_executor():
    spec = get_spec("coder")
    assert "code_executor" in spec.tool_names
    assert "extract_by_attribute" in spec.tool_names
    assert "field_calculator" in spec.tool_names


def test_verifier_has_no_tools():
    spec = get_spec("verifier")
    assert spec.tool_names == []
    assert spec.max_iterations == 2


def test_unknown_role_raises():
    import pytest

    with pytest.raises(KeyError):
        get_spec("nonexistent")
