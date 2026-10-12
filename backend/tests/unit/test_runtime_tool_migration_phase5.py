"""Runtime tool migration closure checks."""

from app.runtime import ToolContractRegistry


def test_runtime_registry_has_handler_and_validator_for_each_contract():
    registry = ToolContractRegistry.with_builtins()
    contracts = list(registry.values())
    assert len(contracts) >= 55
    assert all(contract.handler_ref and contract.validator for contract in contracts)


def test_runtime_contract_operations_are_unique():
    contracts = list(ToolContractRegistry.with_builtins().values())
    assert len({contract.name for contract in contracts}) == len(contracts)
