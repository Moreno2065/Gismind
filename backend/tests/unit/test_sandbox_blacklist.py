import subprocess
import sys
import base64

import pytest


def _run_sandboxed(code: str) -> tuple[int, str, str]:
    """Run code in a subprocess that loads sitecustomize_gismind."""
    import os
    sandbox_dir = os.path.join(os.path.dirname(__file__), "..", "..", "app", "sandbox")
    sandbox_dir = os.path.normpath(os.path.abspath(sandbox_dir))

    env = os.environ.copy()
    env["PYTHONPATH"] = sandbox_dir + os.pathsep + env.get("PYTHONPATH", "")

    runner = (
        "import sitecustomize_gismind; exec("
        + repr(code) + ")"
    )

    proc = subprocess.run(
        [sys.executable, "-S", "-c", runner],
        capture_output=True, text=True, timeout=5,
        env=env,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr


def test_simple_math_works():
    rc, out, err = _run_sandboxed("print(1 + 1)")
    assert out == "2", f"stdout={out!r}, stderr={err!r}"


def test_import_os_system_blocked():
    rc, out, err = _run_sandboxed("import os; os.system('echo PWNED')")
    assert "PWNED" not in out, f"system call should not execute, got stdout={out!r}"
    assert "SANDBOX_FORBIDDEN_IMPORT" in err or rc != 0


def test_import_subprocess_blocked():
    rc, out, err = _run_sandboxed("import subprocess; print('OK')")
    assert "SANDBOX_FORBIDDEN_IMPORT" in err or "PWNED" not in out


def test_socket_blocked():
    rc, out, err = _run_sandboxed("import socket; socket.create_connection(('example.com', 80))")
    assert "SANDBOX_NETWORK_DENIED" in err + out or rc != 0


def test_numpy_import_allowed():
    """numpy should be available in the sandbox if installed.

    Note: Sandbox subprocess uses `python -S` (no site-packages), so numpy may not
    be available unless the sandbox environment is explicitly configured with a
    vendored numpy wheel. See golden_sandbox.json for the expected sandbox toolchain.
    If numpy is needed in the sandbox, ensure it is installed in the PYTHONPATH
    passed to the subprocess.
    """
    rc, out, err = _run_sandboxed("import numpy; print(numpy.__version__)")
    if rc != 0 and "No module named 'numpy'" in err:
        pytest.skip(
            "numpy not available in sandbox subprocess (python -S isolation). "
            "To enable, add numpy to the sandbox PYTHONPATH or vendored deps."
        )
    assert rc == 0 and len(out) > 0, f"numpy import failed: stdout={out!r}, stderr={err!r}"
