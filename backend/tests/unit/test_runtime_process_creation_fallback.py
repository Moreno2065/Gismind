"""Actual Windows process identity fallback, independent of optional psutil."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from app.runtime.queue import _process_creation_time


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='real Windows process API regression')


def test_windows_fallback_matches_actual_current_process_identity(monkeypatch):
    psutil = pytest.importorskip('psutil')
    expected = psutil.Process(os.getpid()).create_time()
    monkeypatch.setitem(sys.modules, 'psutil', None)
    actual = _process_creation_time(os.getpid())
    assert actual is not None and actual > 0
    assert abs(actual - expected) < .001


def test_windows_fallback_matches_actual_child_process_identity(monkeypatch):
    psutil = pytest.importorskip('psutil')
    child = subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(30)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        expected = psutil.Process(child.pid).create_time()
        monkeypatch.setitem(sys.modules, 'psutil', None)
        actual = _process_creation_time(child.pid)
        assert actual is not None and actual > 0
        assert abs(actual - expected) < .001
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_windows_fallback_releases_real_process_query_handles(monkeypatch):
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL

    def count_handles():
        result = wintypes.DWORD()
        assert kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(result))
        return result.value

    monkeypatch.setitem(sys.modules, 'psutil', None)
    before = count_handles()
    for _ in range(32):
        _process_creation_time(os.getpid())
    assert count_handles() <= before
