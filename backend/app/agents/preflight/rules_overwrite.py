"""output_overwrite rule：检查输出路径是否已存在。

注册为 "output_overwrite" 规则，作用于 "export_result" semantic_action。

当输出路径已存在时，返回 warning 级别 issue（不阻断），携带 confirm_overwrite 修复建议。
"""
from __future__ import annotations

import os
from typing import Any

from app.agents.preflight.registry import register_preflight_rule
from app.agents.preflight.validation import ValidationIssue, RepairProposal


@register_preflight_rule("output_overwrite", "export_result")
def _check_output_overwrite(ctx: dict[str, Any]) -> list[ValidationIssue]:
    output_path = ctx.get("kwargs", {}).get("output_path", "")
    if not output_path or not os.path.exists(str(output_path)):
        return []
    return [
        ValidationIssue(
            code="output_exists",
            stage="preflight",
            severity="warning",
            message=f"输出路径 {output_path} 已存在，执行将覆盖。",
            repair=RepairProposal(kind="confirm_overwrite", patch={"overwrite": True}),
        )
    ]
