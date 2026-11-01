import json
import ctypes
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime import db as db_module


def _fake_settings(root: Path):
    data = root / ".gismind"
    return type("Settings", (), {"runtime_paths": {
        "data_root": str(data), "runtime_db": str(data / "runtime.db"),
        "artifact_dir": str(data / "artifacts"), "staging_dir": str(data / "staging"),
        "quarantine_dir": str(data / "quarantine"), "upload_dir": str(data / "uploads"),
    }})()


def test_supervisor_once_initializes_runtime_and_exits(monkeypatch, tmp_path, capsys):
    from app.runtime import supervisor
    monkeypatch.setattr(supervisor, "get_settings", lambda: _fake_settings(tmp_path))

    assert supervisor.main(["--once"]) == 0
    paths = _fake_settings(tmp_path).runtime_paths
    assert Path(paths["runtime_db"]).exists()
    assert Path(paths["data_root"]).joinpath("runtime_paths.json").exists()
    assert "completed" in capsys.readouterr().out


def test_reset_requires_exact_confirmation(tmp_path, monkeypatch):
    from app.runtime import reset
    settings = _fake_settings(tmp_path)
    data = Path(settings.runtime_paths["data_root"])
    data.mkdir(parents=True)
    manifest = dict(settings.runtime_paths)
    (data / "runtime_paths.json").write_text(json.dumps(manifest), encoding="utf-8")
    db = Path(manifest["runtime_db"])
    db.write_bytes(b"db")

    monkeypatch.setattr(reset, "get_settings", lambda: settings)
    assert reset.main([]) != 0
    assert db.exists()


def test_reset_deletes_only_manifest_runtime_targets(tmp_path, monkeypatch, capsys):
    from app.runtime import reset
    settings = _fake_settings(tmp_path)
    paths = settings.runtime_paths
    data = Path(paths["data_root"])
    data.mkdir(parents=True)
    (data / "runtime_paths.json").write_text(json.dumps(paths), encoding="utf-8")
    Path(paths["runtime_db"]).write_bytes(b"db")
    for key in ("artifact_dir", "staging_dir", "quarantine_dir", "upload_dir"):
        p = Path(paths[key]); p.mkdir(parents=True); (p / "payload").write_bytes(b"x")
    outside = tmp_path / "keep.txt"; outside.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(reset, "get_settings", lambda: settings)

    assert reset.main(["--confirm-destructive-reset"]) == 0
    assert not Path(paths["runtime_db"]).exists()
    assert all(not Path(paths[key]).exists() for key in ("artifact_dir", "staging_dir", "quarantine_dir", "upload_dir"))
    assert outside.exists()
    assert "runtime.db" in capsys.readouterr().out


def test_reset_rejects_active_worker_lock(tmp_path, monkeypatch):
    from app.runtime import reset
    settings = _fake_settings(tmp_path)
    paths = settings.runtime_paths
    data = Path(paths["data_root"]); data.mkdir(parents=True)
    (data / "runtime_paths.json").write_text(json.dumps(paths), encoding="utf-8")
    (data / "runtime.worker.lock").write_text('{"pid": 1}', encoding="utf-8")
    monkeypatch.setattr(reset, "get_settings", lambda: settings)
    assert reset.main(["--confirm-destructive-reset"]) != 0
    assert (data / "runtime.worker.lock").exists()


@pytest.mark.parametrize("wait_status, expected", [(0, False), (258, True), (0xFFFFFFFF, None)])
def test_windows_worker_probe_checks_exit_signal_and_closes_handle(monkeypatch, wait_status, expected):
    from app.runtime import supervisor

    opened, waited, closed = [], [], []
    def open_process(access, inherit, pid):
        opened.append((access, inherit, pid))
        return 123
    def wait_process(handle, timeout):
        waited.append((handle, timeout))
        return wait_status
    def close_process(handle):
        closed.append(handle)
        return True
    kernel = SimpleNamespace(OpenProcess=open_process, WaitForSingleObject=wait_process, CloseHandle=close_process)
    monkeypatch.setattr(supervisor, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    assert supervisor._pid_state(42) is expected
    assert opened == [(0x101000, False, 42)]
    assert waited == [(123, 0)]
    assert closed == [123]


@pytest.mark.parametrize("error, expected", [(87, False), (5, None)])
def test_windows_worker_probe_preserves_unknown_open_failures(monkeypatch, error, expected):
    from app.runtime import supervisor

    def open_process(*args):
        return 0
    def close_process(*args):
        pytest.fail("no handle was opened")
    def wait_process(*args):
        pytest.fail("no handle was opened")
    kernel = SimpleNamespace(OpenProcess=open_process, WaitForSingleObject=wait_process, CloseHandle=close_process)
    monkeypatch.setattr(supervisor, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
    assert supervisor._pid_state(42) is expected


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process object lifetime")
def test_exited_windows_worker_is_dead_while_its_parent_still_holds_the_handle():
    from app.runtime import supervisor

    with subprocess.Popen(
        [sys.executable, "-X", "utf8", "-B", "-c", "pass"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ) as child:
        assert child.wait(timeout=10) == 0
        assert supervisor._pid_state(child.pid) is False
