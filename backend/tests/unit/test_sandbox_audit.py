import json
import os
from pathlib import Path

from app.audit.sandbox_audit import record
from app.sandbox.runner import run_in_sandbox


def test_audit_record_writes_log(tmp_path):
    log_file = tmp_path / "test_audit.log"
    # Instead of patching, test directly with our own file path
    from app.audit import sandbox_audit
    audit_path = Path(str(log_file))
    old_path = sandbox_audit.AUDIT_PATH
    try:
        sandbox_audit.AUDIT_PATH = audit_path
        record("user1", "print(1+1)", duration_ms=100)
        assert audit_path.exists()
        lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) >= 1
        entry = json.loads(lines[0])
        assert entry["user_id"] == "user1"
        assert entry["code_hash"]  # 16-char hex
    finally:
        sandbox_audit.AUDIT_PATH = old_path


def test_sandbox_call_leaves_audit(tmp_path):
    from app.audit import sandbox_audit
    audit_path = tmp_path / "sandbox_test_audit.log"
    old_path = sandbox_audit.AUDIT_PATH
    try:
        sandbox_audit.AUDIT_PATH = audit_path
        res = run_in_sandbox("print(1+1)", timeout_s=5)
        assert res.returncode == 0, f"expected returncode 0 for successful sandbox run, got {res.returncode}"
        # audit file should have at least one entry
        lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) >= 1
    finally:
        sandbox_audit.AUDIT_PATH = old_path
