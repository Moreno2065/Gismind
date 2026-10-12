"""Phase-five closure checks retained under the Runtime implementation."""

from app.runtime import ToolContractRegistry


def test_phase5_runtime_contract_inventory_is_closed():
    registry = ToolContractRegistry.with_builtins()
    assert all(c.handler_ref.startswith("app.runtime.") for c in registry.values())
    assert all(c.validator.startswith("app.runtime.") for c in registry.values())


def test_phase5_runtime_contract_manifest_is_reconcilable():
    registry = ToolContractRegistry.with_builtins()
    manifest = registry.public_operations_manifest()
    assert manifest["operations"]
    assert all(operation["contract_tests"] for operation in manifest["operations"].values())
