import json
from pathlib import Path

from fastapi.testclient import TestClient


KERNEL = {
    "select_toolkit",
    "inspect_workspace",
    "suggest_skill",
    "load_skill",
    "proactive_clarification",
}


def test_runtime_contracts_exclude_kernel_semantics():
    from app.runtime import ToolContractRegistry

    names = {contract.name for contract in ToolContractRegistry.with_builtins().values()}
    assert names.isdisjoint(KERNEL)


def test_legacy_public_endpoints_are_explicitly_rejected():
    from app.main import create_app
    from app.runtime import RuntimeDB, RuntimeStore

    store = RuntimeStore(RuntimeDB(":memory:"))
    store.initialize()
    with TestClient(create_app(runtime_store=store)) as client:
        for path in ("/api/chat", "/api/upload"):
            response = client.post(path)
            assert response.status_code == 410
            assert response.json()["error"]["code"] == "LEGACY_RUNTIME_REMOVED"


def test_runtime_sessions_do_not_require_redis():
    from app.runtime import RuntimeDB, RuntimeStore

    store = RuntimeStore(RuntimeDB(":memory:"))
    store.initialize()
    session = store.create_session("runtime-session")
    assert session["session_id"] == "runtime-session"
    assert store.get_session("runtime-session")["session_id"] == "runtime-session"


def test_removal_manifest_is_precise_and_not_wildcarded():
    manifest = json.loads((Path(__file__).resolve().parents[3] / "docs" / "runtime_removal_manifest.json").read_text(encoding="utf-8"))
    assert manifest["phase"] == 7
    assert manifest["status"] == "approved-for-delete"
    assert manifest["entries"]
    for entry in manifest["entries"]:
        assert Path(entry["path"]).is_absolute() is False
        assert "*" not in entry["path"]
        assert entry["approved"] is True
        assert entry["replacement_evidence"]


def test_runtime_tree_has_no_executable_v1_residue():
    """The post-delete gate closes both the original and late-discovered V1 paths."""
    from app.runtime.gate import post_delete

    assert post_delete() == []
