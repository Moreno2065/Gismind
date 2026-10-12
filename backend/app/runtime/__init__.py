"""Lazy runtime exports; importing the compiler must not initialize storage."""

__all__ = [
    "ArtifactRef", "ArtifactStore", "AwaitingInputDiagnostic", "CompileError", "CompiledTask",
    "Compiler", "PlanIR", "PlannerDraft", "PlannerStepDraft", "PlannerService", "PlanDraftInvalid", "PortContract", "RUN_STATUSES",
    "TASK_ATTEMPT_STATUSES", "TASK_STATUSES", "RuntimeDB", "RuntimeDatabase", "RuntimeStore",
    "TaskOutputRef", "ToolContract", "ToolContractRegistry", "KernelService", "kernel_service", "compile_plan", "metric_crs_selector", "utc_now",
    "RuntimeExecutor", "RuntimeQueue", "RuntimeSupervisor", "TaskClaim", "RunClaim",
    "OpenAICompatiblePlanner", "PlannerCall", "PlannerError", "PlannerRequest", "CandidateCatalog", "candidate_catalog", "canonical_operation_id", "canonical_operation_ids", "catalog_hash_for_operation_ids",
    "ArtifactPayload", "ArtifactBatch",
]


def __getattr__(name):
    if name in {"ArtifactRef", "CompiledTask", "PlanIR", "PlannerDraft", "PlannerStepDraft", "PortContract", "TaskOutputRef", "ToolContract", "ToolContractRegistry"}:
        from . import contracts
        return getattr(contracts, name)
    if name in {"AwaitingInputDiagnostic", "CompileError", "Compiler", "compile_plan"}:
        from . import compiler
        return getattr(compiler, name)
    if name == "metric_crs_selector":
        from .compiler import metric_crs_selector
        return metric_crs_selector
    if name in {"PlannerService", "PlanDraftInvalid"}:
        from .planner import PlannerService, PlanDraftInvalid
        return {"PlannerService": PlannerService, "PlanDraftInvalid": PlanDraftInvalid}[name]
    if name in {"RUN_STATUSES", "TASK_ATTEMPT_STATUSES", "TASK_STATUSES", "RuntimeDB", "RuntimeDatabase", "utc_now"}:
        from . import db
        return getattr(db, name)
    if name == "RuntimeStore":
        from .store import RuntimeStore
        return RuntimeStore
    if name == "ArtifactStore":
        from .artifacts import ArtifactStore
        return ArtifactStore
    if name == "RuntimeExecutor":
        from .executor import RuntimeExecutor
        return RuntimeExecutor
    if name in {"ArtifactPayload", "ArtifactBatch"}:
        from .output_protocol import ArtifactPayload, ArtifactBatch
        return {"ArtifactPayload": ArtifactPayload, "ArtifactBatch": ArtifactBatch}[name]
    if name in {"RuntimeQueue", "RuntimeSupervisor", "TaskClaim", "RunClaim"}:
        from .queue import RunClaim, RuntimeQueue, RuntimeSupervisor, TaskClaim
        return {"RuntimeQueue": RuntimeQueue, "RuntimeSupervisor": RuntimeSupervisor, "TaskClaim": TaskClaim, "RunClaim": RunClaim}[name]
    if name in {"OpenAICompatiblePlanner", "PlannerCall", "PlannerError", "PlannerRequest", "CandidateCatalog", "candidate_catalog", "canonical_operation_id", "canonical_operation_ids", "catalog_hash_for_operation_ids"}:
        from .llm_planner import CandidateCatalog, OpenAICompatiblePlanner, PlannerCall, PlannerError, PlannerRequest, candidate_catalog, canonical_operation_id, canonical_operation_ids, catalog_hash_for_operation_ids
        return {
            "OpenAICompatiblePlanner": OpenAICompatiblePlanner,
            "PlannerCall": PlannerCall,
            "PlannerError": PlannerError,
            "PlannerRequest": PlannerRequest,
            "CandidateCatalog": CandidateCatalog,
            "candidate_catalog": candidate_catalog,
            "canonical_operation_id": canonical_operation_id,
            "canonical_operation_ids": canonical_operation_ids,
            "catalog_hash_for_operation_ids": catalog_hash_for_operation_ids,
        }[name]
    if name in {"KernelService", "kernel_service"}:
        from .kernel import KernelService, kernel_service
        return {"KernelService": KernelService, "kernel_service": kernel_service}[name]
    raise AttributeError(name)
