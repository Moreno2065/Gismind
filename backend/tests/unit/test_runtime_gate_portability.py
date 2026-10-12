import json

import pytest

from app.runtime import gate


def manifest(tmp_path, monkeypatch, relative, absolute):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "runtime_removal_manifest.json").write_text(json.dumps({
        "phase": 7, "entries": [{"path": relative, "approved": True,
        "absolute_path": absolute, "target_kind": "directory_recursive"}],
    }), encoding="utf-8")
    monkeypatch.setattr(gate, "ROOT", tmp_path)


def test_completed_deletion_evidence_survives_checkout_relocation(tmp_path, monkeypatch):
    manifest(tmp_path, monkeypatch, "blackbox-results.blocked/case-a", r"C:\old\checkout\blackbox-results.blocked\case-a")
    assert gate.post_delete() == []


def test_historical_absolute_path_still_must_identify_same_relative_target(tmp_path, monkeypatch):
    manifest(tmp_path, monkeypatch, "blackbox-results.blocked/case-a", r"C:\old\checkout\unrelated\case-a")
    assert any("absolute path mismatch" in failure for failure in gate.post_delete())


@pytest.mark.parametrize("relative", ["../escape", r"C:\outside", "/outside", "blackbox-results.blocked/../escape", "blackbox-results.blocked/*"])
def test_post_delete_manifest_rejects_cross_platform_escape(tmp_path, monkeypatch, relative):
    manifest(tmp_path, monkeypatch, relative, "unused")
    assert any("not exact relative path" in failure for failure in gate.post_delete())
