"""Structured GIS risk model and decision policy.

Provides GISRisk dataclass, RiskPolicy decision chain, and a taxonomy
for converting ValidationIssues into structured risk objects.
"""

from app.agents.risks.models import GISRisk, RiskDecision, RiskSeverity, RiskDecisionKind
from app.agents.risks.policy import RiskPolicy
from app.agents.risks.taxonomy import (
    CRS_RISK,
    FIELD_RISK,
    EMPTY_RESULT,
    DATA_QUALITY,
    GEOMETRY_RISK,
    LOCATION_DRIFT,
    validation_issue_to_risk,
    result_to_risks,
)

__all__ = [
    "GISRisk",
    "RiskDecision",
    "RiskSeverity",
    "RiskDecisionKind",
    "RiskPolicy",
    "CRS_RISK",
    "FIELD_RISK",
    "EMPTY_RESULT",
    "DATA_QUALITY",
    "GEOMETRY_RISK",
    "LOCATION_DRIFT",
    "validation_issue_to_risk",
    "result_to_risks",
]
