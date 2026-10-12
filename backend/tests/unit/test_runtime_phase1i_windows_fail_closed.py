from __future__ import annotations

import ctypes
import json
import sys

import pytest

from app.runtime import RuntimeSupervisor


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows process handles require ctypes.windll")


def _patch_open_process(monkeypatch, error_code: int) -> None:
    monkeypatch.setattr(ctypes.windll.kernel32, "OpenProcess", lambda *args: 0)
    monkeypatch.setattr(ctypes.windll.kernel32, "GetLastError", lambda: error_code)
    monkeypatch.setitem(sys.modules, "psutil", None)


def test_persisted_windows_process_open_access_denied_is_unresolved(monkeypatch):
    _patch_open_process(monkeypatch, 5)  # ERROR_ACCESS_DENIED

    process, safe = RuntimeSupervisor._persisted_process(
        json.dumps({"pid": 4242, "create_time": 123.5})
    )

    assert process is None
    assert safe is False


def test_persisted_windows_process_open_invalid_parameter_proves_exited(monkeypatch):
    _patch_open_process(monkeypatch, 87)  # ERROR_INVALID_PARAMETER

    process, safe = RuntimeSupervisor._persisted_process(
        json.dumps({"pid": 4242, "create_time": 123.5})
    )

    assert process is None
    assert safe is True
