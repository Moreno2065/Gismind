"""RiskPolicy: decision chain for structured GIS risks.

The policy evaluates a list of GISRisk objects and produces a RiskDecision
following this priority chain:

    1. Any blocking risk       → fail
    2. confirmation_required   → ask_confirmation
    3. auto_repair_available   → auto_repair
    4. Any warning-severity    → warn (proceed with note)
    5. Otherwise               → proceed
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.risks.models import GISRisk, RiskDecision

logger = logging.getLogger(__name__)


class RiskPolicy:
    """Stateless decision engine for GIS risks."""

    def decide(self, risks: list[GISRisk]) -> RiskDecision:
        """Evaluate *risks* and return the highest-priority decision.

        The decision chain (highest priority first):
        1. blocking → fail
        2. confirmation_required → ask_confirmation
        3. auto_repair_available → auto_repair
        4. severity == 'warning' or 'error' → warn
        5. otherwise → proceed
        """
        if not risks:
            return RiskDecision(kind="proceed")

        risk_tuple = tuple(risks)

        # 1. Blocking risks → fail
        blocking = [r for r in risks if r.blocking]
        if blocking:
            primary = max(blocking, key=lambda r: _severity_rank(r.severity))
            logger.info(
                "RiskPolicy: BLOCKING risk %s — %s",
                primary.code, primary.message,
            )
            return RiskDecision(
                kind="fail",
                primary_risk=primary,
                risks=risk_tuple,
            )

        # 2. Confirmation required → ask_confirmation
        confirmations = [r for r in risks if r.confirmation_required]
        if confirmations:
            primary = confirmations[0]
            return RiskDecision(
                kind="ask_confirmation",
                primary_risk=primary,
                risks=risk_tuple,
            )

        # 3. Auto-repair available → auto_repair
        repairable = [r for r in risks if r.auto_repair_available]
        if repairable:
            primary = repairable[0]
            logger.info(
                "RiskPolicy: auto_repair for risk %s",
                primary.code,
            )
            return RiskDecision(
                kind="auto_repair",
                primary_risk=primary,
                risks=risk_tuple,
            )

        # 4. Warning / error severity → warn
        warnings = [r for r in risks if r.severity in ("warning", "error")]
        if warnings:
            primary = max(warnings, key=lambda r: _severity_rank(r.severity))
            return RiskDecision(
                kind="warn",
                primary_risk=primary,
                risks=risk_tuple,
            )

        # 5. Info-level only → proceed
        return RiskDecision(kind="proceed", risks=risk_tuple)


def _severity_rank(severity: str) -> int:
    """Numeric rank for severity comparison (higher = worse)."""
    return {"info": 0, "warning": 1, "error": 2}.get(severity, 0)
