"""验证角色提示词文件的完整性。

确保每个 sub-agent 角色都有对应的提示词文件，
内容足够长（>=200 字符）且角色间内容不相同。
"""

from pathlib import Path

PROMPTS_DIR = Path("app/agents/prompts")
ROLES = ["geo", "poi", "geometer", "viz", "coder", "verifier"]


def test_all_prompt_files_exist():
    for role in ROLES:
        f = PROMPTS_DIR / f"{role}.md"
        assert f.exists(), f"Prompt file missing: {f}"


def test_prompt_files_not_too_short():
    for role in ROLES:
        content = (PROMPTS_DIR / f"{role}.md").read_text(encoding="utf-8")
        assert len(content) > 200, (
            f"{role} prompt too short ({len(content)} chars)"
        )


def test_prompts_are_distinct():
    texts = {
        role: (PROMPTS_DIR / f"{role}.md").read_text(encoding="utf-8")
        for role in ROLES
    }
    # Invert dict: values become keys, duplicate values collapse to one key
    unique = {v: k for k, v in texts.items()}
    assert len(unique) == len(ROLES), (
        f"Expected {len(unique)} distinct prompts, got {len(ROLES)}. "
        f"Duplicate prompt contents detected."
    )


def test_prompt_does_not_advertise_tools_unavailable_to_role():
    """A role prompt must not teach calls that the role cannot execute."""
    from app.agents.planner_factory import build_code_mode_prompt
    prompt = build_code_mode_prompt("geo")
    assert "geo_code(" in prompt
    assert "clip_layer(" not in prompt
    assert "zonal_statistics(" not in prompt
    assert "chart_config(" not in prompt


def test_prompt_uses_direct_values_not_hidden_numeric_indexes():
    """Code mode passes Python values; prompts must not require result indexes."""
    from app.agents.planner_factory import build_code_mode_prompt
    prompt = build_code_mode_prompt("geometer")
    assert "geometry_from=索引" not in prompt
    assert "location_from=索引" not in prompt
    assert "直接使用" in prompt


def test_upload_capable_role_prompt_contains_data_io_read():
    """Uploaded files are loaded through the single host-side read tool."""
    from app.agents.planner_factory import build_code_mode_prompt
    prompt = build_code_mode_prompt("geometer")
    assert "data_io_read(file_id=" in prompt


def test_code_mode_role_knowledge_has_no_legacy_json_contracts():
    for role in ("geo", "poi", "geometer", "viz", "coder"):
        content = (PROMPTS_DIR / f"{role}.md").read_text(encoding="utf-8")
        assert "始终输出纯 JSON" not in content
        assert "geometry_from: 0" not in content
        assert "看不到其他 sub-agent 的结果" not in content
