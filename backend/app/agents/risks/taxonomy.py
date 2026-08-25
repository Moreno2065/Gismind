"""Risk taxonomy: categories, constants, and conversion from ValidationIssue.

Maps existing Gismind error codes and preflight validation issues into
the structured GISRisk model for unified risk handling.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.risks.models import GISRisk

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Risk category constants
# ---------------------------------------------------------------------------

CRS_RISK = "crs_risk"
FIELD_RISK = "field_risk"
EMPTY_RESULT = "empty_result_risk"
DATA_QUALITY = "data_quality_risk"
GEOMETRY_RISK = "geometry_risk"
LOCATION_DRIFT = "location_drift_risk"


# ---------------------------------------------------------------------------
# ValidationIssue → GISRisk conversion
# ---------------------------------------------------------------------------

# Map preflight issue codes to risk categories
_CODE_TO_CATEGORY: dict[str, str] = {
    "buffer_crs_mismatch": CRS_RISK,
    "buffer_requires_projected_crs": CRS_RISK,
    "crs_mismatch": CRS_RISK,
    "field_not_found": FIELD_RISK,
    "missing_required_field": FIELD_RISK,
    "geometry_type_mismatch": GEOMETRY_RISK,
    "empty_geometry": GEOMETRY_RISK,
    "location_drift": LOCATION_DRIFT,
    "empty_result": EMPTY_RESULT,
    "feature_count_anomaly": DATA_QUALITY,
}


def validation_issue_to_risk(issue) -> GISRisk:
    """Convert a preflight ValidationIssue into a GISRisk.

    Args:
        issue: A ValidationIssue (or any object with .code, .severity,
               .message, .stage, .repair attributes).

    Returns:
        GISRisk with fields mapped from the issue.
    """
    code = getattr(issue, "code", "unknown")
    severity_raw = getattr(issue, "severity", "warning")
    severity = "error" if severity_raw == "error" else "warning"
    message = getattr(issue, "message", "")
    stage = getattr(issue, "stage", "preflight")
    repair = getattr(issue, "repair", None)

    category = _CODE_TO_CATEGORY.get(code, DATA_QUALITY)

    auto_repair = False
    repair_action = None
    if repair:
        kind = getattr(repair, "kind", "")
        if kind == "auto_repair":
            auto_repair = True
            repair_action = {
                "action": getattr(repair, "action", ""),
                "patch": getattr(repair, "patch", {}),
            }

    blocking = severity == "error"

    return GISRisk(
        code=code,
        category=category,
        severity=severity,
        message=message,
        technical_detail=f"stage={stage}",
        blocking=blocking,
        auto_repair_available=auto_repair,
        repair_action=repair_action,
    )


# ---------------------------------------------------------------------------
# Tool result → risks (for AFTER_TOOL_CALL hook)
# ---------------------------------------------------------------------------

def result_to_risks(tool_name: str, result: Any) -> list[GISRisk]:
    """Inspect a tool result and produce any detected GISRisks.

    This is called by the AFTER_TOOL_CALL hook. It looks for:
    - status == "error" → blocking risk
    - status == "empty" → info-level empty_result risk
    - error_code matching known categories
    """
    risks: list[GISRisk] = []

    if result is None:
        return risks

    status = ""
    error_code = ""
    message = ""

    # Extract fields from ToolResult or dict
    if hasattr(result, "status"):
        status = result.status
        error_code = getattr(result, "error_code", "") or ""
        message = getattr(result, "message", "") or ""
    elif isinstance(result, dict):
        status = result.get("status", "")
        error_code = result.get("error_code", "") or ""
        message = result.get("message", "") or ""

    if status == "error":
        category = _CODE_TO_CATEGORY.get(error_code, DATA_QUALITY)
        risks.append(GISRisk(
            code=error_code or f"{tool_name}_error",
            category=category,
            severity="error",
            message=message or f"{tool_name} execution failed",
            tool_name=tool_name,
            blocking=True,
        ))
    elif status == "empty":
        risks.append(GISRisk(
            code="empty_result",
            category=EMPTY_RESULT,
            severity="info",
            message=message or f"{tool_name} returned no results",
            tool_name=tool_name,
            blocking=False,
        ))

    return risks
