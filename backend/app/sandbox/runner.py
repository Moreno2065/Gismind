"""隔离子进程 Python 沙箱: subprocess + pywin32 Job Object + 超时 + OOM.

这是"防止误操作 + 资源硬限"的安全网, 不是抗恶意攻击的隔离边界。
"""
from __future__ import annotations
import base64
import json
import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class SandboxResult:
    stdout: str
    stderr: str
    returncode: int
    duration_ms: int
    error_code: Optional[str] = None
    artifacts: Optional[dict] = None


def _classify_sandbox_error(
    stdout: str,
    stderr: str,
    returncode: int,
) -> Optional[str]:
    """Map child streams / exit code to a sandbox error_code.

    Priority:
      1. MemoryError / allocator failure → SANDBOX_OOM
         (must beat forbidden-import sentinel noise from traceback under -S blacklist)
      2. Structured ``__SANDBOX_ERROR__:<code>`` sentinel lines
      3. Non-zero returncode → TOOL_EXECUTION_ERROR
    """
    combined = (stdout or "") + (stderr or "")
    # OOM first: MemoryError is raised by the interpreter; under import blacklist,
    # traceback formatting can also emit SANDBOX_FORBIDDEN_IMPORT noise.
    if "MemoryError" in combined or "Cannot allocate" in combined:
        return "SANDBOX_OOM"

    _SENTINEL = "__SANDBOX_ERROR__:"
    for line in (stderr or "").splitlines():
        if line.startswith(_SENTINEL):
            return line[len(_SENTINEL):].strip()

    if returncode != 0:
        return "TOOL_EXECUTION_ERROR"
    return None


def run_in_sandbox(
    code: str,
    *,
    timeout_s: Optional[int] = None,
    memory_mb: Optional[int] = None,
    sandbox_dir: Optional[str] = None,
    env_overrides: Optional[dict] = None,
) -> SandboxResult:
    """在隔离子进程跑 code, 限制资源与网络.

    Subprocess 使用 PYTHONPATH 加载 sitecustomize_gismind.py (黑名单),
    用 pywin32 Job Object (Windows) 限 memory_mb, 用 Popen.wait(timeout) 超时.

    Windows: CreateProcess with CREATE_SUSPENDED (win32con) → Job Object assign →
    ResumeThread on the primary thread handle (not the process handle).

    Args:
        code: Python code string
        timeout_s: 墙钟超时秒数 (default 60, 硬上限 300)
        memory_mb: 内存硬上限 MB (default 512, 硬上限 2048, Windows only)
        sandbox_dir: 覆盖 sandbox 包路径 (默认自动探测)
        env_overrides: 额外注入子进程的环境变量（如 session_vars 路径 / RPC 端口白名单）

    Returns:
        SandboxResult
    """
    # 从 settings 兜底，仍允许调用方覆盖
    timeout_s = timeout_s if timeout_s is not None else settings.APP_SANDBOX_TIMEOUT_S
    memory_mb = memory_mb if memory_mb is not None else settings.APP_SANDBOX_MEMORY_MB
    # 资源硬上限 — 防止 LLM 或攻击者设置超大值绕过限制
    timeout_s = min(timeout_s, 300)
    memory_mb = min(memory_mb, 2048)
    # 定位 sandbox 包目录
    if sandbox_dir is None:
        sandbox_dir = str(Path(__file__).parent.resolve())

    # 装配 code —— 第一行必须 import sitecustomize_gismind
    wrapped_code = (
        "import sys, base64, json\n"
        "import sitecustomize_gismind\n"
        f"exec({code!r})\n"
    )
    # 输出分隔标记 —— 也包含 base64 编码便于纯文本 stdout 传输
    code_b64 = base64.b64encode(wrapped_code.encode()).decode()

    # 子进程最小 runner
    runner = (
        "import sys, base64, json\n"
        f"code_b64 = {code_b64!r}\n"
        "_code = base64.b64decode(code_b64).decode()\n"
        "try:\n"
        "    exec(compile(_code, '<sandbox>', 'exec'))\n"
        "except SystemExit as _e:\n"
        "    sys.exit(_e.code if _e.code is not None else 0)\n"
        "except Exception as _e:\n"
        "    import traceback\n"
        "    traceback.print_exc()\n"
        "    sys.exit(1)\n"
    )

    import os
    env = os.environ.copy()
    env["PYTHONPATH"] = sandbox_dir + os.pathsep + env.get("PYTHONPATH", "")
    env["APP_SANDBOX_NETWORK_ALLOWLIST"] = settings.APP_SANDBOX_NETWORK_ALLOWLIST
    if env_overrides:
        env.update(env_overrides)

    start = time.time()
    argv = [sys.executable, "-S", "-c", runner]

    # Windows: CreateProcess suspended → Job Object → ResumeThread(thread_handle)
    # Non-Windows: plain Popen (no CREATE_SUSPENDED race window to close).
    if sys.platform == "win32":
        proc = _spawn_windows_suspended(argv, env)
        _apply_job_object(proc, memory_mb)
        _resume_windows_thread(proc)
    else:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        _apply_job_object(proc, memory_mb)

    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
            duration_ms = int((time.time() - start) * 1000)
            # Audit record (Phase 4)
            try:
                from app.audit.sandbox_audit import record as _audit_record
                _audit_record(
                    user_id=threading.current_thread().name or "sandbox",
                    code=code,
                    error_code="SANDBOX_TIMEOUT",
                    duration_ms=duration_ms,
                    returncode=proc.returncode,
                )
            except Exception:
                pass  # audit failure should not crash the sandbox
            return SandboxResult(
                stdout="", stderr="timeout",
                returncode=-1,
                duration_ms=duration_ms,
                error_code="SANDBOX_TIMEOUT",
            )

        duration_ms = int((time.time() - start) * 1000)
        error_code = _classify_sandbox_error(stdout or "", stderr or "", proc.returncode)

        # Audit record (Phase 4)
        try:
            from app.audit.sandbox_audit import record as _audit_record
            _audit_record(
                user_id=threading.current_thread().name or "sandbox",
                code=code,
                error_code=error_code,
                duration_ms=duration_ms,
                returncode=proc.returncode,
            )
        except Exception:
            pass  # audit failure should not crash the sandbox

        return SandboxResult(
            stdout=(stdout or "").strip(),
            stderr=(stderr or "").strip(),
            returncode=proc.returncode,
            duration_ms=duration_ms,
            error_code=error_code,
        )
    finally:
        # Drop Job Object after communicate/kill so KILL_ON_JOB_CLOSE was active
        # for the full child lifetime, then release handles.
        _close_job_handle(proc)
        _close_thread_handle(proc)
        _close_process_handle(proc)


def _build_extended_limit_info(win32job, memory_mb: int, job=None) -> dict:
    """Build JOBOBJECT_EXTENDED_LIMIT_INFORMATION for pywin32.

    ProcessMemoryLimit is a top-level field on ExtendedLimitInformation.
    Nesting it under BasicLimitInformation raises:
      TypeError: JOBOBJECT_BASIC_LIMIT_INFORMATION() takes at most 9 keyword arguments
    """
    # Querying ``None`` means "the current process job" and fails with
    # ERROR_ACCESS_DENIED when Codex/IDE already runs inside a restrictive job.
    # Query a job handle we own instead; its default structure is safe to edit
    # and can be submitted to any newly created sandbox job.
    owned_job = job is None
    query_job = job or win32job.CreateJobObject(None, "")
    try:
        info = win32job.QueryInformationJobObject(
            query_job, win32job.JobObjectExtendedLimitInformation
        )
    finally:
        if owned_job:
            try:
                import win32api

                win32api.CloseHandle(query_job)
            except Exception:
                pass
    # Drop runtime-only fields that must not be re-submitted with extra keys.
    basic = dict(info["BasicLimitInformation"])
    basic.pop("ProcessMemoryLimit", None)
    basic["LimitFlags"] = (
        win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    info["BasicLimitInformation"] = basic
    info["ProcessMemoryLimit"] = int(memory_mb) * 1024 * 1024
    return info


def _close_job_handle(proc) -> None:
    """Release Job Object handle stored on proc (after communicate/cleanup)."""
    job = getattr(proc, "_job", None)
    if job is None:
        return
    try:
        import win32api

        win32api.CloseHandle(job)
    except Exception:
        try:
            # Fallback if win32api unavailable: CloseHandle via kernel32
            import ctypes

            ctypes.windll.kernel32.CloseHandle(int(job))
        except Exception:
            pass
    try:
        delattr(proc, "_job")
    except Exception:
        proc._job = None  # type: ignore[attr-defined]


def _close_thread_handle(proc) -> None:
    """Release primary thread handle kept for ResumeThread."""
    th = getattr(proc, "_thread_handle", None)
    if th is None:
        return
    try:
        import win32api

        win32api.CloseHandle(th)
    except Exception:
        try:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(int(th))
        except Exception:
            pass
    try:
        delattr(proc, "_thread_handle")
    except Exception:
        proc._thread_handle = None  # type: ignore[attr-defined]


def _close_process_handle(proc) -> None:
    """Release process handle from CreateProcess path (_WinProc only)."""
    if not isinstance(proc, _WinProc):
        return
    ph = getattr(proc, "_handle", None)
    if ph is None:
        return
    try:
        import win32api

        win32api.CloseHandle(ph)
    except Exception:
        try:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(int(ph))
        except Exception:
            pass
    proc._handle = None  # type: ignore[attr-defined]

def _resume_windows_thread(proc) -> None:
    """Resume primary thread after Job Object assignment (thread handle, not process)."""
    th = getattr(proc, "_thread_handle", None)
    if th is None:
        logger.warning("No thread handle to resume for suspended sandbox process")
        return
    try:
        import win32process

        # ResumeThread returns previous suspend count (>=1 if still suspended)
        prev = win32process.ResumeThread(th)
        if prev == 0xFFFFFFFF:
            raise OSError("ResumeThread failed")
    except Exception:
        # ctypes fallback with the same thread handle
        try:
            import ctypes

            prev = ctypes.windll.kernel32.ResumeThread(int(th))
            if prev == 0xFFFFFFFF:
                raise OSError("ResumeThread failed via kernel32")
        except Exception:
            logger.exception("Failed to resume suspended sandbox process")


def _spawn_windows_suspended(argv: list[str], env: dict) -> subprocess.Popen:
    """CreateProcess with CREATE_SUSPENDED; return a Popen-compatible process.

    Keeps the primary *thread* handle on ``proc._thread_handle`` so callers can
    AssignProcessToJobObject then ResumeThread(thread_handle) with no race.
    """
    import msvcrt
    import os

    import pywintypes
    import win32api
    import win32con
    import win32file
    import win32pipe
    import win32process

    sa = pywintypes.SECURITY_ATTRIBUTES()
    sa.bInheritHandle = True

    # stdout / stderr pipes (child inherits write ends)
    stdout_r, stdout_w = win32pipe.CreatePipe(sa, 0)
    stderr_r, stderr_w = win32pipe.CreatePipe(sa, 0)
    # Parent read ends must not be inherited
    win32api.SetHandleInformation(stdout_r, win32con.HANDLE_FLAG_INHERIT, 0)
    win32api.SetHandleInformation(stderr_r, win32con.HANDLE_FLAG_INHERIT, 0)

    # stdin → NUL (DEVNULL equivalent)
    nul_handle = win32file.CreateFile(
        "NUL",
        win32con.GENERIC_READ,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
        sa,
        win32con.OPEN_EXISTING,
        0,
        None,
    )

    si = win32process.STARTUPINFO()
    si.dwFlags |= win32con.STARTF_USESTDHANDLES
    # Pass PyHANDLE objects directly — int() conversion can break inheritance.
    si.hStdInput = nul_handle
    si.hStdOutput = stdout_w
    si.hStdError = stderr_w

    creation_flags = (
        win32con.CREATE_SUSPENDED
        | win32con.CREATE_NEW_PROCESS_GROUP
        | win32process.CREATE_UNICODE_ENVIRONMENT
    )
    cmdline = subprocess.list2cmdline(argv)

    try:
        proc_handle, thread_handle, pid, _tid = win32process.CreateProcess(
            None,  # app name (use command line)
            cmdline,
            None,  # process security
            None,  # thread security
            1,  # inherit handles
            creation_flags,
            env,  # environment dict (unicode)
            None,  # cwd
            si,
        )
    except Exception:
        # Close pipe / nul handles on spawn failure
        for h in (stdout_r, stdout_w, stderr_r, stderr_w, nul_handle):
            try:
                win32api.CloseHandle(h)
            except Exception:
                pass
        raise

    # Parent no longer needs write ends / nul; child holds its copies
    for h in (stdout_w, stderr_w, nul_handle):
        try:
            win32api.CloseHandle(h)
        except Exception:
            pass

    # Detach PyHANDLE ownership before open_osfhandle — otherwise when these
    # locals are GC'd they CloseHandle the same pipes the fds own, breaking
    # I/O (and the still-suspended child) as soon as this function returns.
    stdout_fd = msvcrt.open_osfhandle(stdout_r.Detach(), os.O_RDONLY)
    stderr_fd = msvcrt.open_osfhandle(stderr_r.Detach(), os.O_RDONLY)
    stdout_file = os.fdopen(stdout_fd, "r", encoding="utf-8", errors="replace")
    stderr_file = os.fdopen(stderr_fd, "r", encoding="utf-8", errors="replace")

    # Build a real subprocess.Popen shell without re-spawning: use from_handle pattern.
    # CPython does not expose a public constructor for this, so we assemble a
    # minimal Popen-like object that supports communicate / kill / wait / returncode.
    proc = _WinProc(
        proc_handle=proc_handle,
        thread_handle=thread_handle,
        pid=pid,
        stdout=stdout_file,
        stderr=stderr_file,
    )
    return proc  # type: ignore[return-value]


class _WinProc:
    """Minimal Popen-compatible wrapper around CreateProcess handles.

    Exposes communicate/kill/wait/returncode/pid/_handle so the rest of
    ``run_in_sandbox`` stays shared with the Unix path.
    """

    def __init__(self, proc_handle, thread_handle, pid, stdout, stderr):
        self._handle = proc_handle  # process handle (for Job Object / terminate)
        self._thread_handle = thread_handle  # primary thread (for ResumeThread)
        self.pid = pid
        self.stdin = None
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: Optional[int] = None
        self._job = None
        self.args = ()

    def communicate(self, timeout=None):
        """Read stdout/stderr until process exits (or timeout).

        On timeout: kill the child first so pipe readers unblock, then re-raise
        TimeoutExpired so the caller matches subprocess.Popen semantics.
        """
        import threading

        out_chunks: list[str] = []
        err_chunks: list[str] = []

        def _read(stream, sink):
            try:
                data = stream.read()
                if data:
                    sink.append(data)
            except Exception:
                pass

        t_out = threading.Thread(target=_read, args=(self.stdout, out_chunks), daemon=True)
        t_err = threading.Thread(target=_read, args=(self.stderr, err_chunks), daemon=True)
        t_out.start()
        t_err.start()

        timed_out = False
        try:
            self.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Kill before joining readers so EOF unblocks stream.read().
            self.kill()
            try:
                self.wait(timeout=3)
            except Exception:
                pass
        finally:
            t_out.join(timeout=2)
            t_err.join(timeout=2)
            try:
                self.stdout.close()
            except Exception:
                pass
            try:
                self.stderr.close()
            except Exception:
                pass

        if timed_out:
            raise subprocess.TimeoutExpired(
                self.args,
                timeout,
                output="".join(out_chunks),
                stderr="".join(err_chunks),
            )
        return "".join(out_chunks), "".join(err_chunks)

    def wait(self, timeout=None):
        import win32event
        import win32process

        if self.returncode is not None and self._handle is None:
            return self.returncode
        if self._handle is None:
            return self.returncode
        timeout_ms = win32event.INFINITE if timeout is None else max(0, int(float(timeout) * 1000))
        rc = win32event.WaitForSingleObject(self._handle, timeout_ms)
        if rc == win32event.WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self.args, timeout)
        self.returncode = int(win32process.GetExitCodeProcess(self._handle))
        return self.returncode

    def poll(self):
        import win32event
        import win32process

        if self.returncode is not None:
            return self.returncode
        if self._handle is None:
            return self.returncode
        rc = win32event.WaitForSingleObject(self._handle, 0)
        if rc == win32event.WAIT_TIMEOUT:
            return None
        self.returncode = int(win32process.GetExitCodeProcess(self._handle))
        return self.returncode

    def kill(self):
        # Prefer win32process.TerminateProcess; fall back to kernel32.
        if self._handle is None:
            if self.returncode is None:
                self.returncode = 1
            return
        try:
            import win32process

            win32process.TerminateProcess(self._handle, 1)
        except Exception:
            try:
                import win32api

                win32api.TerminateProcess(self._handle, 1)
            except Exception:
                try:
                    import ctypes

                    ctypes.windll.kernel32.TerminateProcess(int(self._handle), 1)
                except Exception:
                    pass
        if self.returncode is None:
            self.returncode = 1

    def terminate(self):
        self.kill()


def _apply_job_object(proc, memory_mb: int) -> None:
    """绑 pywin32 Job Object 限 memory_mb. 非 Windows 直接 pass.

    Job handle is stored on ``proc._job`` for the process lifetime so
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE remains effective until communicate/cleanup.
    """
    if sys.platform != "win32":
        return
    try:
        import win32job
    except ImportError:
        logger.warning("pywin32 not available, Job Object skipped")
        return

    process_handle = None
    opened_via_kernel32 = False
    try:
        # Prefer the process handle we already hold from CreateProcess.
        process_handle = getattr(proc, "_handle", None)
        if not process_handle:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_SET_QUOTA = 0x0100
            PROCESS_TERMINATE = 0x0001
            PROCESS_SET_INFORMATION = 0x0200
            desired_access = PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_SET_INFORMATION
            process_handle = kernel32.OpenProcess(desired_access, False, proc.pid)
            if process_handle:
                opened_via_kernel32 = True
    except Exception:
        process_handle = getattr(proc, "_handle", None)

    if not process_handle:
        logger.warning("No process handle available for Job Object assignment")
        return

    job = None
    try:
        job = win32job.CreateJobObject(None, "")
        info = _build_extended_limit_info(win32job, memory_mb, job=job)
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, info
        )
        win32job.AssignProcessToJobObject(job, process_handle)
        # Keep job alive for the child lifetime (KILL_ON_JOB_CLOSE).
        proc._job = job  # type: ignore[attr-defined]
    except Exception:
        logger.exception("Failed to set Job Object resource limits")
        if job is not None and getattr(proc, "_job", None) is None:
            try:
                import win32api

                win32api.CloseHandle(job)
            except Exception:
                pass
    finally:
        if opened_via_kernel32 and process_handle:
            try:
                import ctypes

                ctypes.windll.kernel32.CloseHandle(process_handle)
            except Exception:
                pass
