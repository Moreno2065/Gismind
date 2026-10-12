"""Planner-side draft parsing without LLM or runtime side effects."""

from __future__ import annotations

import json
from typing import Any

from .contracts import PlannerDraft, PlannerStepDraft


class PlanDraftInvalid(ValueError):
    code = "PLAN_DRAFT_INVALID"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        self.path = "draft"
        self.message = message
        self.details = details or {}
        super().__init__(f"{self.code} at {self.path}: {message}")


def parse_planner_draft(value: PlannerDraft | dict[str, Any] | str) -> PlannerDraft:
    """Parse a JSON/dict draft; schema failures remain explicit to the caller."""
    if isinstance(value, PlannerDraft):
        return value
    if isinstance(value, str):
        value = json.loads(value)
    return PlannerDraft.model_validate(value)


class PlannerService:
    """Pure draft parser with one bounded, caller-supplied repair attempt."""

    def __init__(self, repair_callback=None):
        self.repair_callback = repair_callback

    def parse_and_repair(self, value: PlannerDraft | dict[str, Any] | str) -> PlannerDraft:
        try:
            return parse_planner_draft(value)
        except Exception as first_error:
            if self.repair_callback is None:
                raise PlanDraftInvalid("planner draft does not satisfy the Draft schema", details={"error": str(first_error)}) from first_error
            try:
                repaired = self.repair_callback(value, first_error)
            except Exception as repair_error:
                raise PlanDraftInvalid("planner draft repair failed", details={"error": str(repair_error)}) from repair_error
            try:
                return parse_planner_draft(repaired)
            except Exception as second_error:
                raise PlanDraftInvalid("planner draft does not satisfy the Draft schema after repair", details={"error": str(second_error)}) from second_error


def parse_and_repair(value, repair_callback=None) -> PlannerDraft:
    return PlannerService(repair_callback=repair_callback).parse_and_repair(value)


__all__ = ["PlannerDraft", "PlannerStepDraft", "PlanDraftInvalid", "PlannerService", "parse_planner_draft", "parse_and_repair"]
