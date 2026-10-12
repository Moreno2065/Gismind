"""End-to-end production Runtime corpus.

Every case in this package drives the real production chain:

    natural language goal
      -> candidate_catalog recall
      -> PlannerDraft -> Compiler/PlanIR
      -> durable Queue claim + fenced attempt
      -> RuntimeExecutor + postconditions
      -> CAS Artifact publish
      -> terminal Run + Event Log

The only substituted component is the LLM boundary: a deterministic
``PlannerDraft`` is injected through the same ``PlannerProvider`` interface the
OpenAI-compatible provider uses, so catalog recall, compilation, execution and
semantic validation are all exercised for real.
"""
