import hashlib
import sqlite3
import threading
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore
from app.runtime.db import SCHEMA
from app.runtime.migrations import MIGRATIONS
from app.config import Settings


def test_runtime_db_enables_sqlite_safety_pragmas_and_creates_domain_tables(tmp_path):
    db = RuntimeDB(tmp_path / "runtime.db")
    db.initialize()

    with db.connection() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {
        "sessions", "messages", "message_artifacts", "runs", "run_input_revisions",
        "run_commands", "plans", "tasks", "task_dependencies", "task_input_bindings",
        "task_confirmations", "task_attempts", "task_inputs", "task_outputs",
        "artifacts", "artifact_lineage", "artifact_pins", "artifact_leases",
        "events", "cache_entries", "cache_artifacts",
    } <= tables


def test_runtime_schema_has_plan_ir_and_cache_fingerprint_columns(tmp_path):
    db = RuntimeDB(tmp_path / "runtime.db")
    db.initialize()
    with db.connection() as conn:
        plan_columns = {row[1] for row in conn.execute("PRAGMA table_info(plans)")}
        cache_columns = {row[1] for row in conn.execute("PRAGMA table_info(cache_entries)")}
    assert {"input_revision_id", "ir_version", "compiler_version", "plan_hash", "ir_json"} <= plan_columns
    assert {"contract_version", "execution_fingerprint", "input_fingerprint"} <= cache_columns


def test_run_events_have_contiguous_sequences_and_share_transaction(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1", input_revision=1)

    assert runtime.append_event("r1", "run_started", {"ok": True})["seq"] == 1
    assert runtime.append_event("r1", "step", {})["seq"] == 2
    assert runtime.get_run("r1")["last_event_seq"] == 2

    with pytest.raises(RuntimeError):
        with runtime.transaction():
            runtime.append_event("r1", "will_rollback", {})
            raise RuntimeError("abort")
    assert runtime.list_events("r1")[-1]["seq"] == 2


def test_task_id_is_global_and_task_delete_respects_restrictive_foreign_keys(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one")
    runtime.create_task("t2", "p1", "two")
    with pytest.raises(sqlite3.IntegrityError):
        runtime.create_task("t1", "p1", "duplicate")
    runtime.add_task_dependency("t2", "t1")
    with pytest.raises(sqlite3.IntegrityError):
        runtime.delete_task("t1")


def test_artifact_store_publishes_valid_content_to_sha256_cas_with_lineage(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one")
    store = ArtifactStore(tmp_path / "data", runtime)

    source = store.publish_staged(store.stage_bytes(b"source"), origin="upload")
    staged = store.stage_bytes(b"hello", filename="answer.txt")
    digest = hashlib.sha256(b"hello").hexdigest()
    artifact = store.publish_staged(
        staged, origin="task", producer_task_id="t1", media_type="text/plain", parents=[source["artifact_id"]]
    )
    assert artifact["content_hash"] == digest
    assert artifact["validation_status"] == "valid"
    assert (tmp_path / "data" / "artifacts" / "sha256" / digest[:2] / digest).read_bytes() == b"hello"
    assert runtime.get_artifact(artifact["artifact_id"])["producer_task_id"] == "t1"
    assert runtime.list_lineage(artifact["artifact_id"])


def test_artifact_store_rejects_invalid_origin_and_only_validates_reachable_files(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    staged = store.stage_bytes(b"x", filename="x.bin")
    with pytest.raises(ValueError):
        store.publish_staged(staged, origin="task")

    digest = hashlib.sha256(b"x").hexdigest()
    with pytest.raises(ValueError):
        store.publish_staged(staged, origin="upload", producer_task_id="unexpected")
    assert not runtime.get_artifact(digest)

    candidates = store.gc_candidates()
    assert isinstance(candidates, list)


def test_repository_covers_commands_bindings_attempts_pins_leases_and_cache(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one")
    runtime.create_task("t2", "p1", "two")
    store = ArtifactStore(tmp_path / "data", runtime)
    runtime.create_run_command("c1", "r1", "pause", {"reason": "user"})
    runtime.bind_task_input("b1", "t2", "source", source_task_id="t1")
    runtime.create_task_confirmation("cf1", "t2")
    runtime.create_task_attempt("a1", "t2", 1, lease_generation=3)
    source = store.publish_staged(store.stage_bytes(b"x"), origin="upload")
    runtime.attach_task_input("t2", "source", source["artifact_id"])
    result = store.publish_staged(store.stage_bytes(b"result"), origin="task", producer_task_id="t1")
    runtime.attach_task_output("t1", "result", result["artifact_id"])
    runtime.pin_artifact(source["artifact_id"], "session:s1")
    runtime.lease_artifact(source["artifact_id"], "run:r1", expires_at="2999-01-01T00:00:00Z")
    runtime.put_cache("cache:key")
    runtime.attach_cache_artifact("cache:key", source["artifact_id"])
    assert runtime.get_run_command("c1")["command_type"] == "pause"
    assert runtime.reachable_artifact_ids() == {source["artifact_id"], result["artifact_id"]}


def test_duplicate_uploads_create_distinct_artifacts_over_one_cas_blob(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    first = store.publish_staged(store.stage_bytes(b"same"), origin="upload")
    second = store.publish_staged(store.stage_bytes(b"same"), origin="upload")
    assert first["artifact_id"] != second["artifact_id"]
    assert first["content_hash"] == second["content_hash"]


def test_v11_artifact_identity_migration_preserves_references_and_unblocks_shared_blob(tmp_path):
    path = tmp_path / "runtime.db"
    legacy_db = RuntimeDB(path, migrations={version: migration for version, migration in MIGRATIONS.items() if version <= 10})
    legacy = RuntimeStore(legacy_db)
    legacy.initialize()
    legacy.create_session("s1")
    legacy.append_message("s1", "user", "keep this artifact", message_id="m1")
    old_store = ArtifactStore(tmp_path / "data", legacy)
    original = old_store.publish_staged(
        old_store.stage_bytes(b'{"type":"FeatureCollection","features":[]}', filename="source.geojson"),
        origin="upload",
        artifact_type="Vector",
        crs={"kind": "EPSG", "code": 4326},
    )
    legacy.link_message_artifact("m1", original["artifact_id"])

    RuntimeDB(path).initialize()
    runtime = RuntimeStore(RuntimeDB(path))
    runtime.initialize()
    upgraded = ArtifactStore(tmp_path / "data", runtime)
    sibling = upgraded.publish_staged(
        upgraded.stage_bytes(b'{"type":"FeatureCollection","features":[]}', filename="opaque.bin"),
        origin="upload",
        artifact_type="File",
    )

    assert runtime.get_artifact(original["artifact_id"])["type"] == "Vector"
    assert runtime.get_artifact(sibling["artifact_id"])["type"] == "File"
    assert original["content_hash"] == sibling["content_hash"]
    with runtime.db.connection() as conn:
        assert conn.execute("SELECT artifact_id FROM message_artifacts WHERE message_id='m1'").fetchone()[0] == original["artifact_id"]
        assert conn.execute("SELECT COUNT(*) FROM artifacts WHERE content_hash=?", (original["content_hash"],)).fetchone()[0] == 2
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_gc_never_candidates_a_shared_blob_while_any_semantic_artifact_is_reachable(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    payload = b'{"type":"FeatureCollection","features":[]}'
    retained = store.publish_staged(
        store.stage_bytes(payload, filename="source.geojson"), origin="upload",
        artifact_type="Vector", crs={"kind": "EPSG", "code": 4326},
    )
    sibling = store.publish_staged(
        store.stage_bytes(payload, filename="same-blob.bin"), origin="upload", artifact_type="File",
    )
    runtime._write(lambda c: c.execute("UPDATE artifacts SET created_at='2000-01-01T00:00:00Z'"))
    runtime.pin_artifact(retained["artifact_id"], "retain")

    candidates = store.gc_candidates(grace_period_s=86400)

    assert sibling["artifact_id"] not in {row["artifact_id"] for row in candidates}
    assert store.path_for(retained["artifact_id"]).is_file()


def test_session_delete_is_a_soft_delete_and_run_can_track_active_plan(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1", session_version=4)
    runtime.create_run("r1", "s1", input_revision=2)
    runtime.create_plan("p1", "r1")
    runtime.set_active_plan("r1", "p1")
    runtime.update_run_status("r1", "planning")
    runtime.update_run_status("r1", "ready")
    runtime.update_run_status("r1", "running")
    runtime.delete_session("s1")
    assert runtime.get_session("s1")["deleted_at"] is not None
    assert runtime.get_run("r1")["active_plan_id"] == "p1"
    assert runtime.get_run("r1")["status"] == "running"


def test_invalid_artifact_metadata_cannot_be_published(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    with pytest.raises(ValueError):
        runtime.insert_artifact({
            "artifact_id": "a1", "origin": "upload", "content_hash": "a" * 64,
            "storage_uri": "artifacts/aa/b", "size_bytes": 1, "validation_status": "invalid",
        })


def test_gc_excludes_pins_and_expired_references_are_not_roots(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    first = store.publish_staged(store.stage_bytes(b"one"), origin="upload")
    second = store.publish_staged(store.stage_bytes(b"two"), origin="upload")
    runtime._write(lambda c: c.execute("UPDATE artifacts SET created_at='2000-01-01T00:00:00Z'"))
    runtime.pin_artifact(first["artifact_id"], "manual")
    runtime.put_cache("expired", expires_at="2000-01-01T00:00:00Z")
    runtime.attach_cache_artifact("expired", second["artifact_id"])
    candidates = {row["artifact_id"] for row in store.gc_candidates(grace_period_s=86400)}
    assert first["artifact_id"] not in candidates
    assert second["artifact_id"] in candidates


def test_message_artifact_link_and_migration_marker_are_durable(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    message = runtime.append_message("s1", "user", "hello")
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"hello"), origin="upload")
    runtime.link_message_artifact(message["message_id"], artifact["artifact_id"])
    with runtime.db.connection() as conn:
        from app.runtime.migrations import MIGRATIONS
        assert [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()] == list(MIGRATIONS)
        assert conn.execute("SELECT artifact_id FROM message_artifacts").fetchone()[0] == artifact["artifact_id"]


def test_gc_treats_task_inputs_as_reachable_roots(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one")
    runtime.create_task("t2", "p1", "two")
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"input"), origin="upload")
    runtime.attach_task_input("t2", "source", artifact["artifact_id"])
    candidates = ArtifactStore(tmp_path / "data", runtime).gc_candidates()
    assert artifact["artifact_id"] not in {row["artifact_id"] for row in candidates}


def test_gc_uses_grace_period_and_returns_stable_manifest(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"fresh"), origin="upload")
    runtime._write(lambda c: c.execute("UPDATE artifacts SET created_at='2000-01-01T00:00:00Z'"))
    with pytest.raises(ValueError):
        store.gc_candidates(grace_period_s=3600)
    manifest = store.candidate_manifest(grace_period_s=86400)
    assert manifest["version"] == 1
    assert manifest["candidates"][0]["artifact_id"] == artifact["artifact_id"]
    assert list(manifest) == ["version", "generated_at", "grace_period_s", "candidates"]


def test_expired_lease_is_ignored_but_unpublished_hash_lease_protects_cas(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"leased"), origin="upload")
    runtime._write(lambda c: c.execute("UPDATE artifacts SET created_at='2000-01-01T00:00:00Z'"))
    old = "2000-01-01T00:00:00Z"
    runtime.lease_artifact(artifact["artifact_id"], "old", expires_at=old, purpose="gc")
    digest = hashlib.sha256(b"future").hexdigest()
    runtime.lease_artifact(None, "future", expires_at="2999-01-01T00:00:00Z", purpose="commit", content_hash=digest)
    assert runtime.is_content_hash_reachable(digest) is True
    assert artifact["artifact_id"] in {row["artifact_id"] for row in store.gc_candidates(grace_period_s=86400)}


def test_content_hash_lease_protects_published_artifact_with_logical_id(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    artifact = runtime.create_artifact_record("logical-a1", origin="upload", content=b"hash-lease")
    with pytest.raises(ValueError, match="valid"):
        runtime.lease_artifact(None, "commit-hash", expires_at="2999-01-01T00:00:00Z", purpose="commit", content_hash=artifact["content_hash"])


def test_migration_rejects_unknown_version_without_partial_domain_schema(tmp_path):
    path = tmp_path / "runtime.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_migrations VALUES (99, '2000-01-01T00:00:00Z')")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="unknown migration"):
        RuntimeDB(path).initialize()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='sessions'").fetchone() is None
    conn.close()


def test_each_runtime_connection_sets_synchronous_normal(tmp_path):
    db = RuntimeDB(tmp_path / "runtime.db")
    with db.connection() as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1


def test_input_binding_sources_are_exclusive_and_records_have_ordinals(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one")
    runtime.create_task("t2", "p1", "two")
    with pytest.raises(ValueError):
        runtime.bind_task_input("b0", "t2", "source")
    with pytest.raises(ValueError):
        runtime.bind_task_input("b1", "t2", "source", source_task_id="t1", source_artifact_id="a1")
    binding = runtime.bind_task_input("b2", "t2", "source", source_task_id="t1", ordinal=2)
    assert binding["ordinal"] == 2
    with runtime.db.connection() as conn:
        for table in ("task_input_bindings", "task_inputs", "task_outputs"):
            assert "ordinal" in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_lease_purposes_are_mutually_exclusive(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"x"), origin="upload")
    runtime.lease_artifact(artifact["artifact_id"], "download", expires_at="2999-01-01T00:00:00Z", purpose="download")
    with pytest.raises(RuntimeError):
        runtime.lease_artifact(artifact["artifact_id"], "backup", expires_at="2999-01-01T00:00:00Z", purpose="backup")


def test_run_and_task_statuses_are_enumerated(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    with pytest.raises(ValueError):
        runtime.create_run("r1", "s1", status="bogus")
    runtime.create_run("r1", "s1")
    with pytest.raises(ValueError):
        runtime.update_run_status("r1", "bogus")
    runtime.create_plan("p1", "r1")
    with pytest.raises(ValueError):
        runtime.create_task("t1", "p1", "one", status="bogus")
    runtime.create_task("t1", "p1", "one")
    with pytest.raises(ValueError):
        runtime.append_event("r1", "bad", {}, status="bogus")


def test_runtime_settings_resolve_project_data_and_store_paths(tmp_path):
    settings = Settings(_env_file=None)
    paths = settings.runtime_paths
    assert Path(paths["project_root"]).is_absolute()
    assert Path(paths["data_root"]).is_absolute()
    assert Path(paths["runtime_db"]).is_relative_to(Path(paths["data_root"]))
    assert Path(paths["artifact_dir"]).is_relative_to(Path(paths["data_root"]))
    assert Path(paths["staging_dir"]).is_relative_to(Path(paths["data_root"]))


def test_database_enforces_binding_source_xor(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one")
    runtime.create_task("t2", "p1", "two")
    with runtime.db.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO task_input_bindings(binding_id,task_id,input_name,source_task_id,source_artifact_id) VALUES ('b','t2','x','t1','a')")


def test_failed_publication_is_quarantined_and_staging_boundary_is_enforced(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    staged = store.stage_bytes(b"orphan")
    with pytest.raises(ValueError):
        store.publish_staged(staged, origin="task", producer_task_id="missing")
    assert not list((tmp_path / "data" / "artifacts").rglob("orphan"))
    assert list((tmp_path / "data" / "quarantine").iterdir())
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    with pytest.raises(ValueError):
        store.publish_staged(outside, origin="upload")


def test_artifact_and_hash_lease_paths_persist_same_hash_and_conflict(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"lease"), origin="upload")
    runtime.lease_artifact(artifact["artifact_id"], "by-id", expires_at="2999-01-01T00:00:00Z", purpose="download")
    with runtime.db.connection() as conn:
        row = conn.execute("SELECT artifact_id,content_hash FROM artifact_leases WHERE lease_key='by-id'").fetchone()
        assert row[0] == artifact["artifact_id"]
        assert row[1] == artifact["content_hash"]
    with pytest.raises(RuntimeError):
        runtime.lease_artifact(None, "by-hash", expires_at="2999-01-01T00:00:00Z", purpose="backup", content_hash=artifact["content_hash"])


def test_ordinal_and_source_all_constraints_are_enforced_by_repository_and_db(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one")
    runtime.create_task("t2", "p1", "two")
    with pytest.raises(ValueError):
        runtime.bind_task_input("negative", "t2", "x", source_task_id="t1", ordinal=-1)
    with pytest.raises(ValueError):
        runtime.bind_task_input("negative-source", "t2", "x", source_task_id="t1", source_ordinal=-1)
    with runtime.db.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO task_input_bindings(binding_id,task_id,input_name,source_task_id,ordinal) VALUES ('x','t2','x','t1',-1)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO task_input_bindings(binding_id,task_id,input_name,source_task_id,source_all,source_ordinal) VALUES ('y','t2','y','t1',1,1)")


def test_dependency_resolution_materializes_inputs_and_marks_ready_atomically(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", status="ready")
    runtime.update_task_status("t1", "leased")
    runtime.update_task_status("t1", "running")
    runtime.update_task_status("t1", "validating")
    runtime.create_task_attempt("attempt-t1", "t1", 1)
    runtime.update_task_attempt_status("attempt-t1", "succeeded")
    runtime.update_task_status("t1", "succeeded", validated=True)
    runtime.create_task("t2", "p1", "two", status="ready")
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"output"), origin="task", producer_task_id="t1")
    runtime.attach_task_output("t1", "result", artifact["artifact_id"], ordinal=0)
    runtime.bind_task_input("b1", "t2", "source", source_task_id="t1", source_port="result", source_ordinal=0)
    resolved = runtime.resolve_task_inputs("p1")
    assert resolved["t2"] == 1
    with runtime.db.connection() as conn:
        assert conn.execute("SELECT artifact_id,ordinal FROM task_inputs WHERE task_id='t2'").fetchone()[0] == artifact["artifact_id"]
        assert conn.execute("SELECT status FROM tasks WHERE task_id='t2'").fetchone()[0] == "ready"


def test_run_and_task_state_transitions_use_only_documented_statuses(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1", status="queued")
    runtime.update_run_status("r1", "planning")
    with pytest.raises(ValueError):
        runtime.update_run_status("r1", "queued")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", status="ready")
    runtime.put_cache("cache:status", metadata={"execution_fingerprint": "status-fp", "compiler_version": "1.0.0", "contract_version": "1.0.0", "contract_identity": "status", "input_fingerprint": ["status"]})
    store = ArtifactStore(tmp_path / "data", runtime)
    cached = store.publish_staged(store.stage_bytes(b"status"), origin="upload")
    runtime.attach_cache_artifact("cache:status", cached["artifact_id"])
    runtime.update_task_status("t1", "succeeded", cache_hit=True, cache_key="cache:status", execution_fingerprint="status-fp", compiler_version="1.0.0", contract_version="1.0.0", contract_identity="status", input_fingerprint=["status"])
    with pytest.raises(ValueError):
        runtime.create_run("bad", "s1", status="pending")
    with pytest.raises(ValueError):
        runtime.create_task("bad", "p1", "bad", status="pending")


def test_cache_hit_accepts_a_verified_empty_input_fingerprint(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", status="ready")
    runtime.put_cache("empty-input", metadata={"execution_fingerprint": "fp", "compiler_version": "1", "contract_version": "1", "contract_identity": "op", "input_fingerprint": []})
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"cached"), origin="upload")
    runtime.attach_cache_artifact("empty-input", artifact["artifact_id"])
    runtime.update_task_status("t1", "succeeded", cache_hit=True, cache_key="empty-input", execution_fingerprint="fp", compiler_version="1", contract_version="1", contract_identity="op", input_fingerprint=[])


def test_candidate_manifest_reconfirms_db_references_and_cas_integrity(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"manifest"), origin="upload")
    manifest = store.candidate_manifest()
    runtime.pin_artifact(artifact["artifact_id"], "late-pin")
    assert store.confirm_candidate_manifest(manifest)["candidates"] == []
    runtime._write(lambda c: c.execute("DELETE FROM artifact_pins WHERE pin_key='late-pin'"))
    store.path_for(artifact["artifact_id"]).write_bytes(b"tampered")
    assert store.confirm_candidate_manifest(manifest)["candidates"] == []


def test_runtime_db_runs_injected_contiguous_future_migrations_atomically(tmp_path):
    db = RuntimeDB(tmp_path / "runtime.db", migrations={1: SCHEMA, 2: "CREATE TABLE migration_v2(value TEXT NOT NULL)"})
    db.initialize()
    with db.connection() as conn:
        assert [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()] == [1, 2]
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='migration_v2'").fetchone() is not None


def test_invalid_runtime_configuration_fails_closed_without_data_fallback(monkeypatch, tmp_path):
    from app import config
    bad = Settings(_env_file=None, APP_PROJECT_ROOT=str(tmp_path / "project"), APP_DATA_ROOT=str(tmp_path / "outside"))
    with pytest.raises(ValueError):
        _ = bad.runtime_paths
    monkeypatch.setattr(config, "get_settings", lambda: bad)
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    with pytest.raises(ValueError):
        ArtifactStore(runtime=runtime)


def test_runtime_paths_require_strict_same_volume_check(monkeypatch, tmp_path):
    from app import config
    monkeypatch.setattr(config, "same_volume", lambda _left, _right: False, raising=False)
    settings = Settings(_env_file=None, APP_PROJECT_ROOT=str(tmp_path), APP_DATA_ROOT=str(tmp_path / "data"))
    with pytest.raises(ValueError, match="volume"):
        _ = settings.runtime_paths


def test_migration_rejects_gapped_applied_versions_without_changing_state(tmp_path):
    path = tmp_path / "runtime.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_migrations VALUES (1, '2000-01-01T00:00:00Z')")
    conn.execute("INSERT INTO schema_migrations VALUES (3, '2000-01-01T00:00:00Z')")
    conn.commit()
    conn.close()
    migrations = {1: SCHEMA, 2: "CREATE TABLE v2(value TEXT)", 3: "CREATE TABLE v3(value TEXT)"}
    with pytest.raises(RuntimeError, match="contiguous|gap"):
        RuntimeDB(path, migrations=migrations).initialize()
    conn = sqlite3.connect(path)
    assert [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")] == [1, 3]
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='v2'").fetchone() is None
    conn.close()


def test_run_and_task_transition_matrix_rejects_unlisted_cross_states(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.update_run_status("r1", "planning")
    with pytest.raises(ValueError):
        runtime.update_run_status("r1", "running")
    runtime.update_run_status("r1", "ready")
    runtime.update_run_status("r1", "running")
    runtime.update_run_status("r1", "awaiting_input")
    with pytest.raises(ValueError):
        runtime.update_run_status("r1", "planning")
    runtime.update_run_status("r1", "running")
    runtime.update_run_status("r1", "completed")
    with pytest.raises(ValueError):
        runtime.append_event("r1", "illegal_transition", status="cancelled")
    with pytest.raises(ValueError):
        runtime.update_run_status("r1", "cancelled")

    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", status="blocked")
    runtime.update_task_status("t1", "ready")
    runtime.update_task_status("t1", "leased")
    with pytest.raises(ValueError):
        runtime.update_task_status("t1", "succeeded")
    runtime.update_task_status("t1", "running")
    runtime.update_task_status("t1", "validating")
    runtime.create_task_attempt("attempt-matrix", "t1", 1)
    runtime.update_task_attempt_status("attempt-matrix", "succeeded")
    runtime.update_task_status("t1", "succeeded", validated=True)
    with pytest.raises(ValueError):
        runtime.update_task_status("t1", "retry_wait")


def test_failed_dependency_cancels_downstream_with_upstream_failed_reason(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("upstream", "p1", "upstream", status="ready")
    runtime.update_task_status("upstream", "leased")
    runtime.update_task_status("upstream", "failed")
    runtime.create_task("downstream", "p1", "downstream", status="ready")
    runtime.add_task_dependency("downstream", "upstream")
    runtime.resolve_task_inputs("p1")
    row = runtime._read(lambda c: c.execute("SELECT status,blocked_reason FROM tasks WHERE task_id='downstream'").fetchone())
    assert tuple(row) == ("cancelled", "UPSTREAM_FAILED")


def test_database_requires_task_binding_selector_xor_and_port(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one")
    runtime.create_task("t2", "p1", "two")
    with runtime.db.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO task_input_bindings(binding_id,task_id,input_name,source_task_id,source_all,source_ordinal) VALUES ('x','t2','x','t1',0,NULL)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO task_input_bindings(binding_id,task_id,input_name,source_task_id,source_port,source_all,source_ordinal) VALUES ('y','t2','y','t1','result',1,0)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO task_input_bindings(binding_id,task_id,input_name,source_artifact_id,source_port) VALUES ('z','t2','z','a1','result')")


def test_gc_default_grace_is_one_day_and_candidate_manifest_acquires_gc_lease(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"old"), origin="upload")
    runtime._write(lambda c: c.execute("UPDATE artifacts SET created_at='2000-01-01T00:00:00Z' WHERE artifact_id=?", (artifact["artifact_id"],)))
    manifest = store.candidate_manifest()
    assert manifest["grace_period_s"] == 86400
    with runtime.db.connection() as conn:
        lease = conn.execute("SELECT purpose,content_hash FROM artifact_leases WHERE purpose='gc'").fetchone()
        assert lease[0] == "gc"
        assert lease[1] == artifact["content_hash"]


def test_runtime_paths_are_persisted_as_absolute_json_with_volume_metadata(tmp_path):
    settings = Settings(_env_file=None, APP_PROJECT_ROOT=str(tmp_path), APP_DATA_ROOT=str(tmp_path / "data"))
    paths = settings.runtime_paths
    manifest_path = Path(paths["data_root"]) / "runtime_paths.json"
    assert manifest_path.is_file()
    payload = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["data_root"] == paths["data_root"]
    assert payload["artifact_dir"] == paths["artifact_dir"]
    assert payload["staging_dir"] == paths["staging_dir"]
    assert payload["artifact_volume"]
    assert payload["staging_volume"]


def test_artifact_lease_hash_is_database_not_null(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    artifact = runtime.create_artifact_record("a1", origin="upload", content=b"lease")
    with runtime.db.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO artifact_leases(artifact_id,content_hash,lease_key,purpose,expires_at,created_at) VALUES (?,?,?,?,?,?)",
                (artifact["artifact_id"], None, "bad-null", "download", "2999-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )


def test_existing_nullable_lease_table_is_upgraded_from_artifact_hash(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_migrations VALUES (1, '2026-01-01T00:00:00Z');
        CREATE TABLE artifacts(
            artifact_id TEXT PRIMARY KEY, origin TEXT NOT NULL,
            producer_task_id TEXT, content_hash TEXT NOT NULL UNIQUE,
            storage_uri TEXT NOT NULL, media_type TEXT, size_bytes INTEGER NOT NULL,
            validation_status TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE artifact_leases(
            artifact_id TEXT, content_hash TEXT, lease_key TEXT PRIMARY KEY,
            purpose TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL
        );
    """)
    digest = hashlib.sha256(b"legacy").hexdigest()
    conn.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?)", ("a1", "upload", None, digest, "", None, 6, "valid", "2026-01-01T00:00:00Z"))
    conn.execute("INSERT INTO artifact_leases VALUES (?,?,?,?,?,?)", ("a1", None, "legacy-lease", "download", "2999-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
    conn.commit()
    conn.close()
    RuntimeDB(path).initialize()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT content_hash FROM artifact_leases WHERE lease_key='legacy-lease'").fetchone()[0] == digest
    assert conn.execute("SELECT validation_status FROM artifacts WHERE artifact_id='a1'").fetchone()[0] == "quarantined"
    assert conn.execute("PRAGMA table_info(artifact_leases)").fetchall()[1][3] == 1
    conn.close()


def test_gc_default_excludes_logical_rows_and_requires_verified_cas(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    logical = runtime.create_artifact_record("logical", origin="upload", content=b"logical")
    store = ArtifactStore(tmp_path / "data", runtime)
    cas = store.publish_staged(store.stage_bytes(b"cas"), origin="upload")
    runtime._write(lambda c: c.execute("UPDATE artifacts SET created_at='2000-01-01T00:00:00Z' WHERE artifact_id=?", (logical["artifact_id"],)))
    runtime._write(lambda c: c.execute("UPDATE artifacts SET created_at='2000-01-01T00:00:00Z' WHERE artifact_id=?", (cas["artifact_id"],)))
    candidates = {row["artifact_id"] for row in store.gc_candidates()}
    assert logical["artifact_id"] not in candidates
    assert cas["artifact_id"] in candidates
    store.path_for(cas["artifact_id"]).write_bytes(b"tampered")
    assert store.candidate_manifest()["candidates"] == []


def test_run_and_task_initial_states_and_unmet_dependencies_are_restricted(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    with pytest.raises(ValueError):
        runtime.create_run("bad", "s1", status="ready")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    with pytest.raises(ValueError):
        runtime.create_task("bad", "p1", "bad", status="succeeded")
    runtime.create_task("upstream", "p1", "upstream", status="ready")
    runtime.create_task("downstream", "p1", "downstream", status="ready")
    runtime.add_task_dependency("downstream", "upstream")
    runtime.resolve_task_inputs("p1")
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='downstream'").fetchone()[0]) == "blocked"
    runtime.update_task_status("upstream", "leased")
    runtime.update_task_status("upstream", "running")
    runtime.update_task_status("upstream", "validating")
    runtime.create_task_attempt("attempt-upstream", "upstream", 1)
    runtime.update_task_attempt_status("attempt-upstream", "succeeded")
    runtime.update_task_status("upstream", "succeeded", validated=True)
    runtime.resolve_task_inputs("p1")
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='downstream'").fetchone()[0]) == "ready"


def test_confirm_candidate_manifest_rejects_forged_empty_storage_uri(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"cas"), origin="upload")
    forged = {"version": 1, "grace_period_s": 86400, "candidates": [{**artifact, "storage_uri": ""}]}
    assert store.confirm_candidate_manifest(forged)["candidates"] == []


def test_direct_ready_transition_rejects_unmet_dependency_and_keeps_blocked(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("upstream", "p1", "upstream", status="ready")
    runtime.create_task("downstream", "p1", "downstream", status="blocked")
    runtime.add_task_dependency("downstream", "upstream")
    with pytest.raises(ValueError, match="dependency"):
        runtime.update_task_status("downstream", "ready")
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='downstream'").fetchone()[0]) == "blocked"


def test_confirm_candidate_manifest_rechecks_artifact_age_not_manifest_json(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"fresh"), origin="upload")
    forged = {"version": 1, "generated_at": "2000-01-01T00:00:00Z", "grace_period_s": 86400, "candidates": [artifact]}
    assert store.confirm_candidate_manifest(forged)["candidates"] == []


def test_confirm_candidate_manifest_never_trusts_manifest_zero_grace(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"fresh"), origin="upload")
    forged = {"version": 1, "generated_at": "2000-01-01T00:00:00Z", "grace_period_s": 0, "candidates": [artifact]}
    with pytest.raises(ValueError):
        store.confirm_candidate_manifest(forged)


def test_confirm_candidate_manifest_rejects_nonfinite_or_out_of_range_grace_metadata(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"old-cas"), origin="upload")
    runtime._write(lambda c: c.execute("UPDATE artifacts SET created_at='2000-01-01T00:00:00Z' WHERE artifact_id=?", (artifact["artifact_id"],)))
    for value in (0, 3600, 31536001, float("nan"), float("inf"), float("-inf"), "86400", None):
        forged = {"version": 1, "generated_at": "2000-01-01T00:00:00Z", "grace_period_s": value, "candidates": [artifact]}
        with pytest.raises(ValueError):
            store.confirm_candidate_manifest(forged)
        assert runtime.get_artifact(artifact["artifact_id"])["validation_status"] == "valid"


def test_gc_generation_rejects_grace_outside_controlled_policy(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    for value in (-1, 0, 31536001):
        with pytest.raises(ValueError):
            store.gc_candidates(grace_period_s=value)
        with pytest.raises(ValueError):
            store.candidate_manifest(grace_period_s=value)


def test_direct_ready_rejects_pending_confirmation_and_unresolved_binding(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("confirmed", "p1", "confirmed", status="blocked")
    runtime.create_task_confirmation("cf1", "confirmed")
    with pytest.raises(ValueError, match="confirmation"):
        runtime.update_task_status("confirmed", "ready")
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='confirmed'").fetchone()[0]) == "blocked"

    runtime.create_task("bound", "p1", "bound", status="blocked")
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"input"), origin="upload")
    runtime.bind_task_input("binding", "bound", "source", source_artifact_id=artifact["artifact_id"])
    with pytest.raises(ValueError, match="input"):
        runtime.update_task_status("bound", "ready")
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='bound'").fetchone()[0]) == "blocked"


def test_runtime_paths_are_required_when_configuration_import_fails(monkeypatch, tmp_path):
    from app import config
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    def unavailable():
        raise ImportError("configuration unavailable")
    monkeypatch.setattr(config, "get_settings", unavailable)
    with pytest.raises(ValueError, match="config"):
        RuntimeDB()
    with pytest.raises(ValueError, match="config"):
        ArtifactStore(runtime=runtime)


def test_path_for_revalidates_cas_uri_and_payload(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"cas"), origin="upload")
    with runtime.db.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE artifacts SET storage_uri='uploads/not-cas' WHERE artifact_id=?", (artifact["artifact_id"],))
    store.path_for(artifact["artifact_id"]).write_bytes(b"tampered")
    with pytest.raises(ValueError):
        store.path_for(artifact["artifact_id"])


def test_duplicate_publish_repairs_tampered_existing_cas_atomically(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    first = store.publish_staged(store.stage_bytes(b"same"), origin="upload")
    destination = store.path_for(first["artifact_id"])
    destination.write_bytes(b"tampered")
    repaired = store.publish_staged(store.stage_bytes(b"same"), origin="upload")
    assert repaired["validation_status"] == "valid"
    assert destination.read_bytes() == b"same"


def test_valid_artifacts_require_verified_cas_and_logical_records_are_staged(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", status="ready")
    logical = runtime.create_artifact_record("logical", origin="upload", content=b"logical")
    assert logical["validation_status"] == "staged"
    with pytest.raises(ValueError):
        runtime.attach_task_input("t1", "input", logical["artifact_id"])
    with pytest.raises(ValueError):
        runtime.attach_task_output("t1", "output", logical["artifact_id"])
    with pytest.raises(ValueError):
        runtime.attach_artifact_references(logical["artifact_id"])
    with pytest.raises(ValueError):
        runtime.insert_artifact({
            "artifact_id": "bad", "origin": "upload", "content_hash": "a" * 64,
            "storage_uri": "", "size_bytes": 1, "validation_status": "valid",
        })


def test_artifact_reference_apis_reject_invalid_and_quarantined_artifacts(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", status="ready")
    for status in ("invalid", "quarantined"):
        with pytest.raises(ValueError):
            runtime.insert_artifact({
                "artifact_id": status, "origin": "upload", "content_hash": "b" * 64,
                "storage_uri": "", "size_bytes": 0, "validation_status": status,
            })


def test_sqlite_rejects_non_valid_artifacts_in_reference_tables(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", status="ready")
    message = runtime.append_message("s1", "user", "hello")
    logical = runtime.create_artifact_record("logical", origin="upload", content=b"logical")
    with runtime.db.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO task_inputs(task_id,input_name,artifact_id,ordinal) VALUES ('t1','in',?,0)", (logical["artifact_id"],))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO task_outputs(task_id,output_name,artifact_id,ordinal,created_at) VALUES ('t1','out',?,0,'2026-01-01T00:00:00Z')", (logical["artifact_id"],))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO message_artifacts(message_id,artifact_id) VALUES (?,?)", (message["message_id"], logical["artifact_id"]))


def test_sqlite_reference_guards_cover_valid_empty_uri_and_reference_updates(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", status="ready")
    store = ArtifactStore(tmp_path / "data", runtime)
    valid = store.publish_staged(store.stage_bytes(b"valid"), origin="upload")
    staged = runtime.create_artifact_record("staged", origin="upload", content=b"staged")
    with runtime.db.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO artifacts(artifact_id,origin,content_hash,storage_uri,size_bytes,validation_status,created_at) VALUES (?,?,?,?,?,?,?)",
                ("empty-valid", "upload", "a" * 64, "", 0, "valid", "2026-01-01T00:00:00Z"),
            )
        conn.execute(
            "INSERT INTO task_inputs(task_id,input_name,artifact_id,ordinal) VALUES (?,?,?,0)",
            ("t1", "input", valid["artifact_id"]),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE task_inputs SET artifact_id=? WHERE task_id='t1' AND input_name='input' AND ordinal=0",
                (staged["artifact_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE artifacts SET validation_status='invalid' WHERE artifact_id=?", (valid["artifact_id"],))


def test_ready_to_succeeded_requires_explicit_cache_hit_and_records_atomic_event(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", status="ready")
    with pytest.raises(ValueError):
        runtime.update_task_status("t1", "succeeded")
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='t1'").fetchone()[0]) == "ready"
    runtime.put_cache("cache:explicit", metadata={"execution_fingerprint": "explicit-fp", "compiler_version": "1.0.0", "contract_version": "1.0.0", "contract_identity": "explicit", "input_fingerprint": ["explicit"]})
    store = ArtifactStore(tmp_path / "data", runtime)
    cached = store.publish_staged(store.stage_bytes(b"explicit"), origin="upload")
    runtime.attach_cache_artifact("cache:explicit", cached["artifact_id"])
    runtime.update_task_status("t1", "succeeded", cache_hit=True, cache_key="cache:explicit", execution_fingerprint="explicit-fp", compiler_version="1.0.0", contract_version="1.0.0", contract_identity="explicit", input_fingerprint=["explicit"])
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='t1'").fetchone()[0]) == "succeeded"
    assert runtime.list_events("r1")[0]["payload_json"].find("t1") >= 0


def test_resolve_ready_transition_appends_task_event_in_same_transaction(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", status="blocked")
    runtime.resolve_task_inputs("p1")
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='t1'").fetchone()[0]) == "ready"
    assert runtime.list_events("r1")


def test_session_tombstone_releases_message_artifact_gc_root(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    message = runtime.append_message("s1", "user", "hello")
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"hello"), origin="upload")
    runtime.link_message_artifact(message["message_id"], artifact["artifact_id"])
    runtime._write(lambda c: c.execute("UPDATE artifacts SET created_at='2000-01-01T00:00:00Z' WHERE artifact_id=?", (artifact["artifact_id"],)))
    assert artifact["artifact_id"] in runtime.reachable_artifact_ids()
    runtime.delete_session("s1")
    assert runtime._read(lambda c: c.execute("SELECT COUNT(*) FROM message_artifacts WHERE message_id=?", (message["message_id"],)).fetchone()[0]) == 0
    assert artifact["artifact_id"] not in runtime.reachable_artifact_ids()


def test_cache_hit_requires_live_cache_entry_and_cas_contract(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", status="ready")
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"cached"), origin="upload")
    runtime.put_cache("cache:live", expires_at="2999-01-01T00:00:00Z", metadata={"execution_fingerprint": "live-fp", "compiler_version": "1.0.0", "contract_version": "1.0.0", "contract_identity": "live", "input_fingerprint": ["live"]})
    runtime.attach_cache_artifact("cache:live", artifact["artifact_id"])
    with pytest.raises(ValueError, match="cache_key"):
        runtime.update_task_status("t1", "succeeded", cache_hit=True)
    runtime.update_task_status("t1", "succeeded", cache_hit=True, cache_key="cache:live", execution_fingerprint="live-fp", compiler_version="1.0.0", contract_version="1.0.0", contract_identity="live", input_fingerprint=["live"])
    assert runtime.list_events("r1")[-1]["event_type"] == "task.cache_hit"


def test_artifact_table_persists_type_crs_and_schema_metadata(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    columns = runtime._read(lambda c: {row[1] for row in c.execute("PRAGMA table_info(artifacts)")})
    assert {"type", "crs", "schema"}.issubset(columns)


def test_runtime_artifact_lookup_decodes_typed_metadata(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(
        store.stage_bytes(b'{"type":"FeatureCollection","features":[]}', filename="typed.geojson"),
        origin="upload", artifact_type="Vector", crs={"kind": "EPSG", "code": 4326},
        schema={"fields": {"name": "string"}},
    )
    loaded = runtime.get_artifact(artifact["artifact_id"])
    assert loaded["type"] == "Vector"
    assert loaded["crs"] == {"kind": "EPSG", "code": 4326}
    assert loaded["schema"] == {"fields": {"name": "string"}}


def test_cache_hit_requires_matching_execution_metadata(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", status="ready")
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"cached"), origin="upload")
    metadata = {"execution_fingerprint": "fp-1", "compiler_version": "1.0.0", "contract_version": "1.0.0", "contract_identity": "vector.buffer", "input_fingerprint": [artifact["content_hash"]]}
    runtime.put_cache("cache:metadata", expires_at="2999-01-01T00:00:00Z", metadata=metadata)
    runtime.attach_cache_artifact("cache:metadata", artifact["artifact_id"])
    with pytest.raises(ValueError, match="fingerprint"):
        runtime.update_task_status("t1", "succeeded", cache_hit=True, cache_key="cache:metadata", execution_fingerprint="wrong", compiler_version="1.0.0", contract_version="1.0.0", contract_identity="vector.buffer", input_fingerprint=[artifact["content_hash"]])
    runtime.update_task_status("t1", "succeeded", cache_hit=True, cache_key="cache:metadata", execution_fingerprint="fp-1", compiler_version="1.0.0", contract_version="1.0.0", contract_identity="vector.buffer", input_fingerprint=[artifact["content_hash"]])


def test_cache_hit_rejects_missing_or_expired_cache_contract(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", status="ready")
    runtime.put_cache("cache:expired", expires_at="2000-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="cache"):
        runtime.update_task_status("t1", "succeeded", cache_hit=True, cache_key="cache:expired")


def test_validating_task_requires_submitted_output_or_validated_commit(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", status="ready")
    runtime.update_task_status("t1", "leased")
    runtime.update_task_status("t1", "running")
    runtime.update_task_status("t1", "validating")
    with pytest.raises(ValueError, match="validating|output|attempt"):
        runtime.update_task_status("t1", "succeeded")
    store = ArtifactStore(tmp_path / "data", runtime)
    output = store.publish_staged(store.stage_bytes(b"output"), origin="task", producer_task_id="t1")
    runtime.attach_task_output("t1", "result", output["artifact_id"])
    runtime.update_task_status("t1", "succeeded", validated=True)
    assert runtime.list_events("r1")[-1]["event_type"] == "task.completed"


def test_tombstoned_session_is_a_write_barrier(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    message = runtime.append_message("s1", "user", "before")
    runtime.delete_session("s1")
    with pytest.raises(ValueError, match="SESSION_DELETED"):
        runtime.append_message("s1", "user", "after")
    with pytest.raises(ValueError, match="SESSION_DELETED"):
        runtime.create_run("r2", "s1")
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"message"), origin="upload")
    with pytest.raises(ValueError, match="SESSION_DELETED"):
        runtime.link_message_artifact(message["message_id"], artifact["artifact_id"])


def test_gc_grace_configuration_import_failure_is_fail_closed(monkeypatch, tmp_path):
    from app import config
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    monkeypatch.setattr(config, "get_settings", lambda: (_ for _ in ()).throw(ImportError("missing settings")))
    with pytest.raises(ValueError, match="grace|configuration"):
        store.gc_candidates()


def test_commit_validated_task_requires_output_or_succeeded_attempt(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", status="ready")
    runtime.update_task_status("t1", "leased")
    runtime.update_task_status("t1", "running")
    runtime.update_task_status("t1", "validating")
    with pytest.raises(ValueError, match="output|attempt|validated"):
        runtime.commit_validated_task("t1")
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='t1'").fetchone()[0]) == "validating"
    assert runtime.list_events("r1")[-1]["event_type"] == "task.status_changed"


def test_duplicate_publish_concurrency_never_quarantines_canonical_cas(tmp_path, monkeypatch):
    db_path = tmp_path / "runtime.db"
    runtime1 = RuntimeStore(RuntimeDB(db_path))
    runtime2 = RuntimeStore(RuntimeDB(db_path))
    runtime1.initialize()
    barrier = threading.Barrier(2)
    store1 = ArtifactStore(tmp_path / "data", runtime1)
    store2 = ArtifactStore(tmp_path / "data", runtime2)
    original_validate = ArtifactStore._validate

    def synchronized_validate(self, path, expected_hash=None, *, artifact_type="Artifact"):
        result = original_validate(self, path, expected_hash, artifact_type=artifact_type)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(ArtifactStore, "_validate", synchronized_validate)
    results = []
    errors = []

    def publish(store):
        try:
            results.append(store.publish_staged(store.stage_bytes(b"same"), origin="upload"))
        except Exception as exc:  # assertion below checks neither path quarantines CAS
            errors.append(exc)

    threads = [threading.Thread(target=publish, args=(store,)) for store in (store1, store2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    digest = hashlib.sha256(b"same").hexdigest()
    assert all(runtime1.get_artifact(result["artifact_id"])["validation_status"] == "valid" for result in results)
    assert store1.path_for(results[0]["artifact_id"]).read_bytes() == b"same"
    assert not any(path.name.startswith(digest) for path in store1.quarantine_dir.iterdir())


def test_gc_confirmation_does_not_hold_sqlite_write_lock_while_hashing(tmp_path, monkeypatch):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    artifact = store.publish_staged(store.stage_bytes(b"large-enough"), origin="upload")
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat().replace("+00:00", "Z")
    with runtime.db.connection() as conn:
        conn.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?", (old, artifact["artifact_id"]))
    manifest = store.candidate_manifest()
    canonical = store.cas_path(artifact["content_hash"])
    original_read_bytes = Path.read_bytes
    read_started = threading.Event()
    release_read = threading.Event()

    def blocked_read(path):
        if path == canonical:
            read_started.set()
            if not release_read.wait(timeout=5):
                raise AssertionError("test did not release the CAS read")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", blocked_read)
    confirmed = []
    confirm_errors = []
    writer_errors = []
    writer_done = threading.Event()

    def confirm():
        try:
            confirmed.append(store.confirm_candidate_manifest(manifest))
        except Exception as exc:  # pragma: no cover - assertion below reports it
            confirm_errors.append(exc)

    def writer():
        try:
            runtime.update_session("s1")
        except Exception as exc:
            writer_errors.append(exc)
        finally:
            writer_done.set()

    confirm_thread = threading.Thread(target=confirm)
    confirm_thread.start()
    assert read_started.wait(timeout=3)
    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    assert writer_done.wait(timeout=1), "GC CAS hashing held a SQLite write lock"
    release_read.set()
    confirm_thread.join(timeout=10)
    writer_thread.join(timeout=10)
    assert confirm_errors == []
    assert writer_errors == []
    assert [row["artifact_id"] for row in confirmed[0]["candidates"]] == [artifact["artifact_id"]]


def test_eight_publishers_can_create_a_new_cas_prefix_concurrently(tmp_path, monkeypatch):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    stores = [ArtifactStore(tmp_path / "data", runtime) for _ in range(8)]
    payload = b"first-prefix-race"
    digest = hashlib.sha256(payload).hexdigest()
    prefix = stores[0].artifacts_dir / "sha256" / digest[:2]
    assert not prefix.exists()
    real_resolve = Path.resolve

    def flaky_resolve(path, *args, **kwargs):
        # Reproduce the Windows first-create window: resolving a missing
        # canonical path can transiently fail while another publisher creates
        # its prefix directory.
        if path.name == digest and not path.exists():
            raise ValueError("simulated concurrent resolve race")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", flaky_resolve)
    results = []
    errors = []

    def publish(store):
        try:
            results.append(store.publish_staged(store.stage_bytes(payload), origin="upload"))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=publish, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == [], "\n\n".join(
        f"{exc!r}; errno={getattr(exc, 'errno', None)}; "
        f"winerror={getattr(exc, 'winerror', None)}; "
        f"filename={getattr(exc, 'filename', None)!r}; "
        f"filename2={getattr(exc, 'filename2', None)!r}\n"
        + "".join(traceback.format_exception(exc))
        for exc in errors
    )
    assert len(results) == 8
    assert all(runtime.get_artifact(result["artifact_id"])["validation_status"] == "valid" for result in results)
    assert stores[0].cas_path(digest).is_file()
    assert not any(path.name.startswith(digest) for path in stores[0].quarantine_dir.iterdir())


def test_gc_explicit_grace_requires_finite_numeric_value(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    for value in ("86400", float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="grace"):
            store.gc_candidates(grace_period_s=value)


def test_custom_artifact_directory_uses_logical_cas_uri_consistently(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    custom = tmp_path / "data" / "custom-cas"
    store = ArtifactStore(tmp_path / "data", runtime, artifact_dir=custom)
    artifact = store.publish_staged(store.stage_bytes(b"custom"), origin="upload")
    assert artifact["storage_uri"] == f"artifacts/sha256/{artifact['content_hash'][:2]}/{artifact['content_hash']}"
    assert (custom / "sha256" / artifact["content_hash"][:2] / artifact["content_hash"]).is_file()
    assert store.path_for(artifact["artifact_id"]).read_bytes() == b"custom"


def test_validated_commit_checks_every_task_output_not_only_the_first(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", status="ready")
    runtime.update_task_status("t1", "leased")
    runtime.update_task_status("t1", "running")
    runtime.update_task_status("t1", "validating")
    store = ArtifactStore(tmp_path / "data", runtime)
    first = store.publish_staged(store.stage_bytes(b"first"), origin="task", producer_task_id="t1")
    second = store.publish_staged(store.stage_bytes(b"second"), origin="task", producer_task_id="t1")
    runtime.attach_task_output("t1", "a", first["artifact_id"])
    runtime.attach_task_output("t1", "b", second["artifact_id"])
    store.path_for(second["artifact_id"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="CAS|hash|output"):
        runtime.commit_validated_task("t1")
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='t1'").fetchone()[0]) == "validating"


def test_artifact_lease_expiry_requires_utc_rfc3339_and_is_normalized(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    with pytest.raises(ValueError, match="expires_at"):
        runtime.lease_artifact(None, "bad", expires_at="not-a-date", content_hash="a" * 64)
    with pytest.raises(ValueError, match="expires_at"):
        runtime.lease_artifact(None, "naive", expires_at="2999-01-01T00:00:00", content_hash="b" * 64)
    runtime.lease_artifact(None, "offset", expires_at="2999-01-01T08:00:00+08:00", content_hash="c" * 64)
    row = runtime._read(lambda c: c.execute("SELECT expires_at FROM artifact_leases WHERE lease_key='offset'").fetchone())
    assert row[0].endswith("Z") and "08:00:00" not in row[0]


def test_update_run_status_always_appends_one_status_event_atomically(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.update_run_status("r1", "planning")
    events = runtime.list_events("r1")
    assert len(events) == 1
    assert events[0]["event_type"] == "run.status_changed"
    assert '"status": "planning"' in events[0]["payload_json"]


def test_active_attempt_and_pending_confirmation_are_unique_per_task(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", status="ready")
    runtime.create_task_attempt("a1", "t1", 1, status="running")
    with pytest.raises((sqlite3.IntegrityError, ValueError), match="active|attempt"):
        runtime.create_task_attempt("a2", "t1", 2, status="running")
    runtime.create_task_confirmation("c1", "t1")
    with pytest.raises((sqlite3.IntegrityError, ValueError), match="pending|confirmation"):
        runtime.create_task_confirmation("c2", "t1")


def test_repeated_dependency_resolution_does_not_emit_duplicate_status_events(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", status="blocked")
    runtime.resolve_task_inputs("p1")
    first = len(runtime.list_events("r1"))
    runtime.resolve_task_inputs("p1")
    assert len(runtime.list_events("r1")) == first


def test_candidate_confirmation_rejects_non_mapping_manifest_and_candidates(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    store = ArtifactStore(tmp_path / "data", runtime)
    with pytest.raises(ValueError, match="manifest"):
        store.confirm_candidate_manifest([])
    with pytest.raises(ValueError, match="candidate"):
        store.confirm_candidate_manifest({"version": 1, "grace_period_s": 86400, "candidates": ["bad"]})


def test_artifact_origin_is_strict_and_task_producer_must_exist(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    with pytest.raises(ValueError, match="origin"):
        runtime.create_artifact_record("other", origin="derived", content=b"x")
    store = ArtifactStore(tmp_path / "data", runtime)
    staged = store.stage_bytes(b"unknown-origin")
    with pytest.raises(ValueError, match="origin"):
        store.publish_staged(staged, origin="derived")
    assert not list((tmp_path / "data" / "quarantine").iterdir())
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    with pytest.raises(sqlite3.IntegrityError):
        with runtime.db.connection() as conn:
            conn.execute(
                "INSERT INTO artifacts(artifact_id,origin,producer_task_id,content_hash,storage_uri,size_bytes,validation_status,created_at) VALUES (?,?,?,?,?,?,?,?)",
                ("bad", "derived", "missing", "a" * 64, "artifacts/sha256/aa/" + "a" * 64, 0, "staged", "2026-01-01T00:00:00Z"),
            )


def test_task_output_and_binding_dependency_ownership_are_enforced(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_run("r2", "s1")
    runtime.create_plan("p2", "r2")
    runtime.create_task("t1", "p1", "one", status="ready")
    runtime.create_task("t2", "p1", "two", status="ready")
    runtime.create_task("t3", "p2", "three", status="ready")
    with pytest.raises(ValueError, match="producer"):
        ArtifactStore(tmp_path / "data", runtime).publish_staged(
            ArtifactStore(tmp_path / "data", runtime).stage_bytes(b"out"), origin="task", producer_task_id="missing"
        )
    store = ArtifactStore(tmp_path / "data", runtime)
    output = store.publish_staged(store.stage_bytes(b"out"), origin="task", producer_task_id="t1")
    with pytest.raises(ValueError, match="producer"):
        runtime.attach_task_output("t2", "wrong", output["artifact_id"])
    with pytest.raises(ValueError, match="plan"):
        runtime.bind_task_input("cross", "t2", "in", source_task_id="t3", source_port="result")
    with pytest.raises(ValueError, match="plan"):
        runtime.add_task_dependency("t2", "t3")


def test_task_dependencies_reject_self_and_cycles(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("a", "p1", "a", status="ready")
    runtime.create_task("b", "p1", "b", status="ready")
    with pytest.raises(ValueError, match="self"):
        runtime.add_task_dependency("a", "a")
    runtime.add_task_dependency("b", "a")
    with pytest.raises(ValueError, match="cycle"):
        runtime.add_task_dependency("a", "b")


def test_pin_and_lease_by_id_require_valid_cas_artifact(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    staged = runtime.create_artifact_record("staged", origin="upload", content=b"staged")
    with pytest.raises(ValueError, match="valid"):
        runtime.pin_artifact(staged["artifact_id"], "pin")
    with pytest.raises(ValueError, match="valid"):
        runtime.lease_artifact(staged["artifact_id"], "lease", expires_at="2999-01-01T00:00:00Z")
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    store = ArtifactStore(tmp_path / "data", runtime)
    valid = store.publish_staged(store.stage_bytes(b"valid"), origin="upload")
    store.path_for(valid["artifact_id"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="CAS|hash"):
        runtime.lease_artifact(valid["artifact_id"], "lease2", expires_at="2999-01-01T00:00:00Z")


def test_bindings_and_sqlite_outputs_cannot_bypass_artifact_ownership(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("producer", "p1", "producer", status="ready")
    runtime.create_task("consumer", "p1", "consumer", status="ready")
    staged = runtime.create_artifact_record("staged", origin="upload", content=b"staged")
    with pytest.raises(ValueError, match="valid"):
        runtime.bind_task_input("staged-binding", "consumer", "input", source_artifact_id=staged["artifact_id"])
    store = ArtifactStore(tmp_path / "data", runtime)
    output = store.publish_staged(store.stage_bytes(b"output"), origin="task", producer_task_id="producer")
    with runtime.db.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO task_outputs(task_id,output_name,artifact_id,ordinal,created_at) VALUES (?,?,?,?,?)",
                ("consumer", "wrong", output["artifact_id"], 0, "2026-01-01T00:00:00Z"),
            )


def test_dependency_resolution_rejects_conflicting_materialized_input(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("up", "p1", "up", status="ready")
    runtime.create_task("down", "p1", "down", status="blocked")
    runtime.add_task_dependency("down", "up")
    runtime.update_task_status("up", "leased")
    runtime.update_task_status("up", "running")
    runtime.update_task_status("up", "validating")
    store = ArtifactStore(tmp_path / "data", runtime)
    produced = store.publish_staged(store.stage_bytes(b"produced"), origin="task", producer_task_id="up")
    runtime.attach_task_output("up", "result", produced["artifact_id"])
    runtime.update_task_status("up", "succeeded")
    wrong = store.publish_staged(store.stage_bytes(b"wrong"), origin="upload")
    runtime.attach_task_input("down", "input", wrong["artifact_id"], ordinal=0)
    runtime.bind_task_input("binding", "down", "input", source_task_id="up", source_port="result", source_ordinal=0)
    with pytest.raises(ValueError, match="conflict"):
        runtime.resolve_task_inputs("p1")
    assert runtime._read(lambda c: c.execute("SELECT status FROM tasks WHERE task_id='down'").fetchone()[0]) == "blocked"


def test_confirmation_and_attempt_statuses_and_numbers_are_strict(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", status="ready")
    with pytest.raises(ValueError, match="confirmation"):
        runtime.create_task_confirmation("bad", "t1", status="bogus")
    with pytest.raises(ValueError, match="attempt"):
        runtime.create_task_attempt("bad", "t1", -1)
    with pytest.raises(ValueError, match="generation"):
        runtime.create_task_attempt("bad2", "t1", 1, lease_generation=-1)
    with pytest.raises(ValueError, match="attempt"):
        runtime.create_task_attempt("bad3", "t1", 1, status="bogus")


def test_confirmation_and_attempt_status_updates_reject_invalid_values(tmp_path):
    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    runtime.create_session("s1")
    runtime.create_run("r1", "s1")
    runtime.create_plan("p1", "r1")
    runtime.create_task("t1", "p1", "one", status="ready")
    runtime.create_task_confirmation("c1", "t1")
    runtime.create_task_attempt("a1", "t1", 0)
    runtime.update_task_confirmation_status("c1", "confirmed")
    runtime.update_task_attempt_status("a1", "succeeded")
    with pytest.raises(ValueError, match="confirmation"):
        runtime.update_task_confirmation_status("c1", "bogus")
    with pytest.raises(ValueError, match="attempt"):
        runtime.update_task_attempt_status("a1", "bogus")
