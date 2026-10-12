"""A new Artifact identity must not replace healthy, shared CAS bytes."""

import hashlib
import os
import sys
from pathlib import Path

import pytest

from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore


@pytest.fixture
def cas_store(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    return runtime, ArtifactStore(tmp_path / "data", runtime)


def _record_replacements(monkeypatch):
    calls = []
    real_replace = os.replace

    def record(source, destination, *args, **kwargs):
        calls.append((Path(source), Path(destination)))
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", record)
    return calls


def _artifact_ids(runtime):
    with runtime.db.connection() as connection:
        return {
            row[0]
            for row in connection.execute("SELECT artifact_id FROM artifacts")
        }


def test_new_upload_reuses_healthy_cas_without_replacing_independent_metadata(
    cas_store, monkeypatch
):
    runtime, store = cas_store
    payload = b"healthy-shared-cas-bytes"
    first = store.publish_staged(
        store.stage_bytes(payload), origin="upload", schema={"source": "first"}
    )
    canonical = store.path_for(first["artifact_id"])
    first_record = runtime.get_artifact(first["artifact_id"])
    replacements = _record_replacements(monkeypatch)
    staged = store.stage_bytes(payload)

    second = store.publish_staged(
        staged, origin="upload", schema={"source": "second"}
    )

    assert first["artifact_id"] != second["artifact_id"]
    assert first["content_hash"] == second["content_hash"] == hashlib.sha256(payload).hexdigest()
    assert runtime.get_artifact(first["artifact_id"]) == first_record
    assert runtime.get_artifact(second["artifact_id"])["schema"] == {"source": "second"}
    assert _artifact_ids(runtime) == {first["artifact_id"], second["artifact_id"]}
    assert store.path_for(second["artifact_id"]) == canonical
    assert canonical.read_bytes() == payload
    assert not staged.exists()
    assert list(canonical.parent.iterdir()) == [canonical]
    assert replacements == [], "healthy shared CAS bytes must not be replaced"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows denies replacing an open read handle")
def test_new_upload_can_share_cas_while_a_windows_reader_remains_open(cas_store):
    runtime, store = cas_store
    payload = b"reader-must-keep-the-original-cas-bytes"
    first = store.publish_staged(store.stage_bytes(payload), origin="upload")
    first_record = runtime.get_artifact(first["artifact_id"])
    canonical = store.path_for(first["artifact_id"])
    staged = store.stage_bytes(payload)

    with canonical.open("rb") as reader:
        assert reader.read() == payload
        reader.seek(0)
        second = store.publish_staged(staged, origin="upload")
        assert not reader.closed
        assert reader.read() == payload
        assert canonical.read_bytes() == payload

    assert first["artifact_id"] != second["artifact_id"]
    assert first["content_hash"] == second["content_hash"]
    assert runtime.get_artifact(first["artifact_id"]) == first_record
    assert runtime.get_artifact(second["artifact_id"])["validation_status"] == "valid"
    assert _artifact_ids(runtime) == {first["artifact_id"], second["artifact_id"]}
    assert store.path_for(second["artifact_id"]) == canonical
    assert not staged.exists()
    assert not list(store.quarantine_dir.iterdir())

@pytest.mark.parametrize("damage", ["missing", "corrupt_same_size", "corrupt_different_size"])
def test_new_upload_still_atomically_repairs_missing_or_corrupt_shared_cas(
    cas_store, monkeypatch, damage
):
    runtime, store = cas_store
    payload = b"canonical-original"
    first = store.publish_staged(store.stage_bytes(payload), origin="upload")
    first_record = runtime.get_artifact(first["artifact_id"])
    canonical = store.path_for(first["artifact_id"])
    if damage == "missing":
        canonical.unlink()
    elif damage == "corrupt_same_size":
        canonical.write_bytes(b"x" * len(payload))
    else:
        canonical.write_bytes(b"short")
    replacements = _record_replacements(monkeypatch)
    staged = store.stage_bytes(payload)

    second = store.publish_staged(staged, origin="upload")

    assert replacements == [(staged, canonical)]
    assert first["artifact_id"] != second["artifact_id"]
    assert runtime.get_artifact(first["artifact_id"]) == first_record
    assert _artifact_ids(runtime) == {first["artifact_id"], second["artifact_id"]}
    assert store.path_for(first["artifact_id"]).read_bytes() == payload
    assert store.path_for(second["artifact_id"]) == canonical
    assert hashlib.sha256(canonical.read_bytes()).hexdigest() == first["content_hash"]
    assert canonical.stat().st_size == len(payload)
    assert not staged.exists()
    assert not list(store.quarantine_dir.iterdir())


def test_failed_new_metadata_does_not_quarantine_another_artifacts_shared_cas(cas_store):
    runtime, store = cas_store
    payload = b"another-artifact-still-owns-these-bytes"
    first = store.publish_staged(store.stage_bytes(payload), origin="upload")
    first_record = runtime.get_artifact(first["artifact_id"])
    canonical = store.path_for(first["artifact_id"])
    staged = store.stage_bytes(payload)

    # This is a real metadata error after the new Artifact INSERT, not a fake
    # storage failure: an upload has no producer to own a task output.
    with pytest.raises(ValueError, match="task output requires producer_task_id"):
        store.publish_staged(staged, origin="upload", output_name="result")

    assert _artifact_ids(runtime) == {first["artifact_id"]}
    assert runtime.get_artifact(first["artifact_id"]) == first_record
    assert store.path_for(first["artifact_id"]) == canonical
    assert canonical.read_bytes() == payload
    assert not list(store.quarantine_dir.iterdir())
