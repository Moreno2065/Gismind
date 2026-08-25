"""ValidationIssue + RepairProposal + PreflightError dataclasses。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

RepairKind = Literal["ask_user", "confirm_action", "auto_repair", "confirm_overwrite"]
IssueStage = Literal["preflight", "postflight"]
Severity = Literal["error", "warning"]


@dataclass
class RepairProposal:
    """修复建议。

    Attributes:
        kind: 修复类型。
            - ask_user: 需要用户提供更多信息。
            - confirm_action: 建议自动执行某个 action，需用户确认。
            - auto_repair: 自动修复，无需用户确认。
            - confirm_overwrite: 确认覆盖已有输出。
        action: 修复动作标识（如 "reproject_layer"）。
        patch: 修复参数（如 {"input_ref": "new_name"}）。
    """
    kind: RepairKind
    action: str | None = None
    patch: dict[str, Any] | None = None


@dataclass
class ValidationIssue:
    """单个验证问题。

    Attributes:
        code: 问题代码（如 "buffer_crs_mismatch"）。
        stage: 验证阶段（preflight / postflight）。
        severity: 严重程度（error=阻断, warning=警告）。
        message: 给 LLM 看的自然语言（中文优先）。
        repair: 修复建议，None 表示无建议。
    """
    code: str
    stage: IssueStage
    severity: Severity
    message: str
    repair: RepairProposal | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "severity": self.severity,
            "message": self.message,
            "repair": {
                "kind": self.repair.kind,
                "action": self.repair.action,
                "patch": self.repair.patch,
            } if self.repair else None,
        }


class PreflightError(RuntimeError):
    """携带 issues 的异常；被现有 EXECUTION_ERROR 路径捕获。

    LLM 看到 traceback 中包含的 issues 描述后，可以按 self-repair 流程修改代码。
    """

    def __init__(self, message: str, issues: list[ValidationIssue]) -> None:
        super().__init__(message)
        self.issues = issues
