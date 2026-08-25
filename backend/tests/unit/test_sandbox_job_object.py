"""Windows Job Object resource limits for app.sandbox.runner.

Covers:
- ExtendedLimitInformation construction (ProcessMemoryLimit is top-level)
- SetInformationJobObject must not TypeError on JOBOBJECT_BASIC_LIMIT_INFORMATION
- PROCESS_MEMORY + KILL_ON_JOB_CLOSE flags applied
- job handle kept alive on the process until communicate/cleanup
"""
from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Job Object is Windows-only")

win32job = pytest.importorskip("win32job")


def _spawn_sleep_proc() -> subprocess.Popen:
    """Short-lived child we can assign to a job (not suspended)."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_build_extended_limit_info_places_process_memory_at_top_level():
    """ProcessMemoryLimit must live on ExtendedLimitInformation, not BasicLimitInformation.

    Nesting it under BasicLimitInformation causes:
    TypeError: JOBOBJECT_BASIC_LIMIT_INFORMATION() takes at most 9 keyword arguments (10 given)
    """
    from app.sandbox.runner import _build_extended_limit_info

    memory_mb = 64
    info = _build_extended_limit_info(win32job, memory_mb)

    assert "ProcessMemoryLimit" in info
    assert info["ProcessMemoryLimit"] == memory_mb * 1024 * 1024
    assert "ProcessMemoryLimit" not in info["BasicLimitInformation"]

    flags = info["BasicLimitInformation"]["LimitFlags"]
    assert flags & win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
    assert flags & win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE


def test_set_extended_limit_information_does_not_typeerror():
    """SetInformationJobObject with our built info must succeed (no TypeError)."""
    from app.sandbox.runner import _build_extended_limit_info

    job = win32job.CreateJobObject(None, "")
    try:
        info = _build_extended_limit_info(win32job, 32)
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, info
        )
        queried = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation
        )
        assert queried["ProcessMemoryLimit"] == 32 * 1024 * 1024
        flags = queried["BasicLimitInformation"]["LimitFlags"]
        assert flags & win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
        assert flags & win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    finally:
        # Closing job without assigned processes is fine
        try:
            import win32api

            win32api.CloseHandle(job)
        except Exception:
            pass


def test_apply_job_object_keeps_handle_alive_on_proc():
    """job handle must be stored on the Popen so KILL_ON_JOB_CLOSE stays valid."""
    from app.sandbox.runner import _apply_job_object

    proc = _spawn_sleep_proc()
    try:
        memory_mb = 128
        _apply_job_object(proc, memory_mb)

        job = getattr(proc, "_job", None)
        assert job is not None, "proc._job must hold the Job Object handle for process lifetime"

        queried = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation
        )
        assert queried["ProcessMemoryLimit"] == memory_mb * 1024 * 1024
        flags = queried["BasicLimitInformation"]["LimitFlags"]
        assert flags & win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
        assert flags & win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    finally:
        proc.kill()
        proc.wait(timeout=5)
        # After process exit, cleanup may release job
        job = getattr(proc, "_job", None)
        if job is not None:
            try:
                import win32api

                win32api.CloseHandle(job)
            except Exception:
                pass
            try:
                delattr(proc, "_job")
            except Exception:
                pass


def test_apply_job_object_does_not_swallow_typeerror_silently(caplog):
    """Regression: previous code logged TypeError and skipped limits.

    After fix, applying limits to a live process must not log the
    JOBOBJECT_BASIC_LIMIT_INFORMATION keyword TypeError.
    """
    import logging

    from app.sandbox.runner import _apply_job_object

    proc = _spawn_sleep_proc()
    try:
        with caplog.at_level(logging.ERROR, logger="app.sandbox.runner"):
            _apply_job_object(proc, 64)
        joined = "\n".join(r.message for r in caplog.records)
        assert "JOBOBJECT_BASIC_LIMIT_INFORMATION" not in joined
        assert "takes at most 9 keyword arguments" not in joined
        assert getattr(proc, "_job", None) is not None
    finally:
        proc.kill()
        proc.wait(timeout=5)
        job = getattr(proc, "_job", None)
        if job is not None:
            try:
                import win32api

                win32api.CloseHandle(job)
            except Exception:
                pass


def test_run_in_sandbox_attaches_and_cleans_job_handle():
    """End-to-end: run_in_sandbox succeeds with Job Object wiring; no TypeError path."""
    import logging

    from app.sandbox.runner import run_in_sandbox

    cap_records: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            cap_records.append(record)

    handler = _ListHandler()
    handler.setLevel(logging.ERROR)
    logger = logging.getLogger("app.sandbox.runner")
    logger.addHandler(handler)
    try:
        result = run_in_sandbox("print('job-ok')", timeout_s=15, memory_mb=128)
    finally:
        logger.removeHandler(handler)

    assert result.returncode == 0, result
    assert "job-ok" in result.stdout
    messages = "\n".join(r.getMessage() for r in cap_records)
    assert "JOBOBJECT_BASIC_LIMIT_INFORMATION" not in messages
    assert "Failed to set Job Object resource limits" not in messages


def test_classify_prefers_memoryerror_over_forbidden_import_noise():
    """GAP2: MemoryError must classify as SANDBOX_OOM even if forbidden-import sentinel appears.

    Traceback formatting under the import blacklist can emit SANDBOX_FORBIDDEN_IMPORT
    noise; root-cause classification must still prefer interpreter MemoryError.
    """
    from app.sandbox.runner import _classify_sandbox_error

    stderr = (
        "__SANDBOX_ERROR__:SANDBOX_FORBIDDEN_IMPORT\n"
        "Traceback (most recent call last):\n"
        '  File "<sandbox>", line 3, in <module>\n'
        "MemoryError\n"
    )
    assert _classify_sandbox_error(stdout="", stderr=stderr, returncode=1) == "SANDBOX_OOM"
