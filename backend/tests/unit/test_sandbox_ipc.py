import inspect
import platform
import sys

import pytest

from app.sandbox.runner import run_in_sandbox, SandboxResult


def test_simple_code():
    res = run_in_sandbox("print(1 + 1)", timeout_s=5)
    assert res.returncode in (0, None)
    assert "2" in res.stdout


def test_syntax_error():
    res = run_in_sandbox("print(1 +", timeout_s=5)
    # syntax error should not crash the runner; stderr should have trace
    assert res.stderr is not None


def test_timeout():
    res = run_in_sandbox("import time; time.sleep(999)", timeout_s=1)
    assert res.error_code == "SANDBOX_TIMEOUT"
    assert res.duration_ms < 5000  # 1s timeout + 3s kill = <5s


def test_forbidden_import():
    res = run_in_sandbox("import os; print(os.listdir())", timeout_s=5)
    assert res.error_code == "SANDBOX_FORBIDDEN_IMPORT"


@pytest.mark.skipif(
    platform.system() != "Windows",
    reason="OOM capping uses pywin32 Job Object API (Windows-only). "
           "On Linux, an equivalent would use resource.setrlimit(RLIMIT_AS) "
           "or cgroups v2 memory limits, but that is not yet implemented.",
)
def test_oom_capped():
    """Hard memory limit must fire and classify as SANDBOX_OOM (not None / forbidden import).

    Allocates in chunks so ProcessMemoryLimit can trip after partial progress.
    Must not accept None or mis-classify MemoryError as SANDBOX_FORBIDDEN_IMPORT.
    """
    code = (
        "chunks = []\n"
        "for i in range(100):\n"
        "    chunks.append(bytearray(5 * 1024 * 1024))\n"
        "print('ALL_OK')\n"
    )
    res = run_in_sandbox(code, memory_mb=32, timeout_s=20)
    assert res.error_code == "SANDBOX_OOM", (
        f"expected SANDBOX_OOM, got {res.error_code!r}; "
        f"rc={res.returncode}; stdout={res.stdout!r}; stderr={res.stderr!r}"
    )
    assert res.returncode != 0
    assert "ALL_OK" not in (res.stdout or "")
    # MemoryError path must not be swallowed as forbidden-import noise
    assert res.error_code != "SANDBOX_FORBIDDEN_IMPORT"


@pytest.mark.skipif(sys.platform != "win32", reason="CREATE_SUSPENDED is Windows-only")
def test_runner_has_no_dead_subprocess_create_suspended_path():
    """GAP1: dead subprocess.CREATE_SUSPENDED / ResumeThread(process_handle) must be gone.

    Real path uses CreateProcess(+CREATE_SUSPENDED) and ResumeThread(thread_handle).
    """
    from app.sandbox import runner as runner_mod

    src = inspect.getsource(runner_mod)
    # Dead path used hasattr(subprocess, ...) + subprocess.CREATE_SUSPENDED
    assert 'hasattr(subprocess, "CREATE_SUSPENDED")' not in src
    assert "subprocess.CREATE_SUSPENDED" not in src
    assert "win32con.CREATE_SUSPENDED" in src
    assert "CreateProcess" in src
    assert "ResumeThread" in src
    # Must not resume via process handle only
    assert "ResumeThread(proc._handle)" not in src
    assert "ResumeThread(process_handle)" not in src
    # Must keep and resume the primary thread handle
    assert "_thread_handle" in src

@pytest.mark.skipif(sys.platform != "win32", reason="Job Object race window is Windows-only")
def test_job_limit_applies_before_user_code_alloc():
    """Suspended spawn: first large alloc still hits Job Object (no race window)."""
    code = (
        "x = bytearray(80 * 1024 * 1024)\n"
        "print('ALLOC_OK', len(x))\n"
    )
    res = run_in_sandbox(code, memory_mb=16, timeout_s=15)
    assert res.error_code == "SANDBOX_OOM", (
        f"expected SANDBOX_OOM under 16MB job, got {res.error_code!r}; "
        f"rc={res.returncode}; stdout={res.stdout!r}; stderr={res.stderr!r}"
    )
    assert "ALLOC_OK" not in (res.stdout or "")
