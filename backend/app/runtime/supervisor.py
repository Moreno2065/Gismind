"""Standalone Runtime worker supervisor command."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
import signal
import threading
from pathlib import Path

from app.config import get_settings
from .artifacts import ArtifactStore
from .db import RuntimeDB
from .queue import RuntimeSupervisor, _process_creation_time
from .store import RuntimeStore

WORKER_LOCK = "runtime.worker.lock"
WORKER_GUARD = "runtime.worker.guard"


def _pid_state(pid: int) -> bool | None:
    """Return live/dead/unknown without treating permission errors as death."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            kernel.CloseHandle(handle)
            return True
        return False if ctypes.get_last_error() == 87 else None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_owner_active(lock: Path) -> bool:
    try:
        owner = json.loads(lock.read_text(encoding="utf-8"))
        pid = owner["pid"]
        if type(pid) is not int or pid <= 0:
            raise ValueError("invalid worker PID")
        created = owner.get("create_time")
        if created is not None and (not isinstance(created, (int, float)) or isinstance(created, bool)
                                    or not math.isfinite(created) or created <= 0):
            raise ValueError("invalid worker creation time")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"worker lock cannot be verified: {lock}") from exc
    live = _pid_state(pid)
    if live is None:
        raise RuntimeError(f"worker lock owner cannot be verified: {lock}")
    if not live:
        return False
    if created is not None:
        actual = _process_creation_time(pid)
        if actual is None:
            raise RuntimeError(f"worker lock creation time cannot be verified: {lock}")
        return abs(actual - float(created)) < 1.0
    return True


@contextmanager
def _worker_lock(lock: Path):
    """Serialize CLI ownership with an OS lock that is released on hard kill."""
    guard = lock.with_name(WORKER_GUARD)
    with guard.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError(f"active worker lock exists: {lock}") from exc
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError(f"active worker lock exists: {lock}") from exc
        try:
            if lock.is_symlink():
                raise RuntimeError(f"worker lock cannot be verified: {lock}")
            if lock.exists() and _lock_owner_active(lock):
                raise RuntimeError(f"active worker lock exists: {lock}")
            lock.write_text(json.dumps({
                "pid": os.getpid(), "create_time": _process_creation_time(os.getpid()),
            }), encoding="utf-8")
            try:
                yield
            finally:
                lock.unlink(missing_ok=True)
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _paths():
    paths = dict(get_settings().runtime_paths)
    root = Path(paths["data_root"]).resolve()
    paths["worker_lock"] = str(root / WORKER_LOCK)
    root.mkdir(parents=True, exist_ok=True)
    (root / "runtime_paths.json").write_text(json.dumps(paths, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return paths


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Start the Gismind Runtime worker")
    parser.add_argument("--once", action="store_true", help="initialize and process at most one queued task, then exit")
    args = parser.parse_args(argv)
    paths = _paths()
    runtime = RuntimeStore(RuntimeDB(paths["runtime_db"])); runtime.initialize()
    artifacts = ArtifactStore(paths["data_root"], runtime, artifact_dir=paths["artifact_dir"], staging_dir=paths["staging_dir"], quarantine_dir=paths["quarantine_dir"], upload_dir=paths["upload_dir"])
    supervisor = RuntimeSupervisor(runtime, artifacts)
    if args.once:
        completed = supervisor.worker_loop(f"cli-{os.getpid()}", max_tasks=1)
        print(json.dumps({"mode": "once", "completed": completed}))
        return 0
    lock = Path(paths["worker_lock"])
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError): pass
    with _worker_lock(lock):
        supervisor.start(f"cli-{os.getpid()}")
        try:
            while not stop.wait(0.25): pass
        finally:
            supervisor.stop(timeout=5)
    print(json.dumps({"mode": "service", "status": "stopped"}))
    return 0


if __name__ == "__main__":
    main()
