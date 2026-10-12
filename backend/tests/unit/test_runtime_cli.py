import json
from pathlib import Path

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
