"""Structured GIS risk contracts used across validation and runtime stages.

Inspired by PineFlow's risk model. Each risk carries enough information
for the RiskPolicy to decide: proceed / warn / ask_user / auto_repair / fail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

RiskSeverity = Literal["info", "warning", "error"]
RiskDecisionKind = Literal[
    "proceed",
    "warn",
    "ask_user",
    "ask_confirmation",
    "auto_repair",
    "fail",
]


@dataclass
class GISRisk:
    """A single structured risk detected during tool execution."""

    code: str
    category: str
    severity: RiskSeverity
    message: str
    technical_detail: str = ""
    tool_name: str = ""
    blocking: bool = False
    auto_repair_available: bool = False
    repair_action: dict[str, Any] | None = None
    confirmation_required: bool = False
    suggested_choices: list[dict[str, Any]] = field(default_factory=list)
    affects_result_trust: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category,
            "severity": self.severity,
            "message": self.message,
            "technical_detail": self.technical_detail,
            "tool_name": self.tool_name,
            "blocking": self.blocking,
            "auto_repair_available": self.auto_repair_available,
            "repair_action": self.repair_action or {},
            "confirmation_required": self.confirmation_required,
            "suggested_choices": list(self.suggested_choices),
            "affects_result_trust": self.affects_result_trust,
        }


@dataclass(frozen=True)
class RiskDecision:
    """Outcome of the RiskPolicy decision chain."""

    kind: RiskDecisionKind
    primary_risk: GISRisk | None = None
    risks: tuple[GISRisk, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "primary_risk": self.primary_risk.to_dict() if self.primary_risk else {},
            "risks": [r.to_dict() for r in self.risks],
        }
