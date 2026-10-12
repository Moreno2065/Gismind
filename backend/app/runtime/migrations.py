"""Public migration entry point kept separate for deploy/startup callers."""

from .db import SCHEMA


def _v2_plan_cache_columns(conn) -> None:
    for table, additions in {
        "plans": (("input_revision_id", "TEXT"), ("ir_version", "TEXT"), ("compiler_version", "TEXT"), ("plan_hash", "TEXT"), ("ir_json", "TEXT")),
        "cache_entries": (("contract_version", "TEXT"), ("execution_fingerprint", "TEXT"), ("input_fingerprint", "TEXT")),
    }.items():
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
            continue
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in additions:
            if name not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _v3_artifact_metadata_and_lease_ddl(conn) -> None:
    """One-time typed artifact/lease upgrade for pre-runtime databases."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'").fetchone():
        columns = {row[1] for row in conn.execute("PRAGMA table_info(artifacts)")}
        for name, definition in (("type", "TEXT NOT NULL DEFAULT 'Artifact'"), ("crs", "TEXT NOT NULL DEFAULT '{}'"), ("schema", "TEXT NOT NULL DEFAULT '{}'")):
            if name not in columns:
                conn.execute(f"ALTER TABLE artifacts ADD COLUMN {name} {definition}")
                conn.execute("UPDATE artifacts SET validation_status='quarantined' WHERE validation_status='valid'")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifact_leases'").fetchone():
        info = conn.execute("PRAGMA table_info(artifact_leases)").fetchall()
        hash_info = next((row for row in info if row[1] == "content_hash"), None)
        if hash_info is not None and not hash_info[3]:
            conn.execute("UPDATE artifact_leases SET content_hash=(SELECT content_hash FROM artifacts WHERE artifacts.artifact_id=artifact_leases.artifact_id) WHERE content_hash IS NULL AND artifact_id IS NOT NULL")
            if conn.execute("SELECT 1 FROM artifact_leases WHERE content_hash IS NULL LIMIT 1").fetchone():
                raise RuntimeError("cannot upgrade artifact leases with unresolved content hashes")
            conn.execute("ALTER TABLE artifact_leases RENAME TO artifact_leases_legacy")
            conn.execute("""CREATE TABLE artifact_leases (
                artifact_id TEXT REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
                content_hash TEXT NOT NULL,
                lease_key TEXT NOT NULL,
                purpose TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(lease_key), CHECK(artifact_id IS NOT NULL OR content_hash IS NOT NULL),
                CHECK(purpose IN ('commit','download','backup','gc'))
            )""")
            conn.execute("INSERT INTO artifact_leases(artifact_id,content_hash,lease_key,purpose,expires_at,created_at) SELECT artifact_id,content_hash,lease_key,purpose,expires_at,created_at FROM artifact_leases_legacy")
            conn.execute("DROP TABLE artifact_leases_legacy")


def _v4_confirmation_action_hash(conn) -> None:
    """Add the immutable action binding used by side-effect confirmations."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_confirmations'").fetchone():
        columns = {row[1] for row in conn.execute("PRAGMA table_info(task_confirmations)")}
        if "action_hash" not in columns:
            conn.execute("ALTER TABLE task_confirmations ADD COLUMN action_hash TEXT")
    # A content-addressed artifact may be produced by the first task that
    # published it while later tasks in the same plan still need their own
    # output link.  Keep the ownership guard, but scope it to the plan rather
    # than requiring artifact.producer_task_id == output.task_id.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_outputs'").fetchone():
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(task_outputs)")}
    if "link_origin" not in columns:
        conn.execute("ALTER TABLE task_outputs ADD COLUMN link_origin TEXT NOT NULL DEFAULT 'producer'")
    conn.execute("DROP TRIGGER IF EXISTS task_outputs_owner_guard")
    conn.execute("DROP TRIGGER IF EXISTS task_outputs_owner_update_guard")
    conn.execute("""CREATE TRIGGER task_outputs_owner_guard BEFORE INSERT ON task_outputs
       WHEN (NEW.link_origin='producer' AND NOT EXISTS (SELECT 1 FROM artifacts WHERE artifact_id=NEW.artifact_id AND producer_task_id=NEW.task_id))
         OR (NEW.link_origin='dedup' AND NOT EXISTS (
         SELECT 1 FROM artifacts a JOIN tasks producer ON producer.task_id=a.producer_task_id
         JOIN tasks consumer ON consumer.task_id=NEW.task_id
         WHERE a.artifact_id=NEW.artifact_id AND producer.plan_id=consumer.plan_id))
       BEGIN SELECT RAISE(ABORT, 'task output producer must belong to task plan'); END""")


def _v5_task_claim_leases(conn) -> None:
    """Add durable queue claim fields to pre-existing task tables."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone():
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    for name, definition in (("lease_owner", "TEXT"), ("lease_token", "TEXT"), ("lease_expires_at", "TEXT")):
        if name not in columns:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
    conn.execute("""CREATE TRIGGER task_outputs_owner_update_guard BEFORE UPDATE OF task_id,artifact_id,link_origin ON task_outputs
       WHEN (NEW.link_origin='producer' AND NOT EXISTS (SELECT 1 FROM artifacts WHERE artifact_id=NEW.artifact_id AND producer_task_id=NEW.task_id))
         OR (NEW.link_origin='dedup' AND NOT EXISTS (
         SELECT 1 FROM artifacts a JOIN tasks producer ON producer.task_id=a.producer_task_id
         JOIN tasks consumer ON consumer.task_id=NEW.task_id
         WHERE a.artifact_id=NEW.artifact_id AND producer.plan_id=consumer.plan_id))
       BEGIN SELECT RAISE(ABORT, 'task output producer must belong to task plan'); END""")


def _v6_retry_schedule(conn) -> None:
    """Persist retry scheduling decisions instead of busy-looping an attempt."""
    for table, additions in {
        "tasks": (("next_retry_at", "TEXT"),),
        "task_attempts": (("next_retry_at", "TEXT"), ("error_code", "TEXT")),
    }.items():
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
            continue
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in additions:
            if name not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _v7_worker_fencing_and_executor_identity(conn) -> None:
    """Persist planner fencing and child identity across worker restarts."""
    for table, additions in {
        "runs": (
            ("planner_lease_owner", "TEXT"),
            ("planner_lease_token", "TEXT"),
            ("planner_lease_expires_at", "TEXT"),
            ("planner_generation", "INTEGER NOT NULL DEFAULT 0"),
        ),
        "task_attempts": (
            ("executor_identity", "TEXT"),
        ),
    }.items():
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
            continue
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in additions:
            if name not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _v8_executor_identity_not_null(conn) -> None:
    """Quarantine legacy attempts, then make executor identity mandatory."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_attempts'").fetchone():
        return
    info = conn.execute("PRAGMA table_info(task_attempts)").fetchall()
    identity = next((row for row in info if row[1] == "executor_identity"), None)
    if identity is None:
        conn.execute("ALTER TABLE task_attempts ADD COLUMN executor_identity TEXT NOT NULL DEFAULT 'quarantine:missing'")
        return
    conn.execute(
        """UPDATE task_attempts
           SET status=CASE WHEN status IN ('queued','leased','running','validating','retry_wait') THEN 'failed' ELSE status END,
               finished_at=CASE WHEN status IN ('queued','leased','running','validating','retry_wait') THEN COALESCE(finished_at, CURRENT_TIMESTAMP) ELSE finished_at END,
               error_code=COALESCE(error_code, 'MISSING_EXECUTOR_IDENTITY'),
               executor_identity='quarantine:' || attempt_id
           WHERE executor_identity IS NULL OR trim(executor_identity)=''"""
    )
    if identity[3]:
        return
    conn.execute("ALTER TABLE task_attempts RENAME TO task_attempts_legacy_identity")
    conn.execute("""CREATE TABLE task_attempts (
        attempt_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
        attempt_no INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',
        lease_generation INTEGER NOT NULL DEFAULT 0,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        error TEXT,
        error_code TEXT,
        next_retry_at TEXT,
        executor_identity TEXT NOT NULL,
        UNIQUE(task_id, attempt_no),
        CHECK(status IN ('queued','leased','running','validating','succeeded','failed','cancelled','retry_wait')),
        CHECK(attempt_no >= 0),
        CHECK(lease_generation >= 0)
    )""")
    conn.execute("""INSERT INTO task_attempts(attempt_id,task_id,attempt_no,status,lease_generation,started_at,finished_at,error,error_code,next_retry_at,executor_identity)
       SELECT attempt_id,task_id,attempt_no,status,lease_generation,started_at,finished_at,error,error_code,next_retry_at,executor_identity
       FROM task_attempts_legacy_identity""")
    conn.execute("DROP TABLE task_attempts_legacy_identity")


def _v9_planner_attempts(conn) -> None:
    """Persist LLM planning provenance independently from task attempts.

    A planning lease may be reclaimed after a process dies.  The attempt row
    gives operators a durable answer to which fenced planner generation made
    (or rejected) a particular PlanIR without turning the event projection
    into the only source of truth.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS planner_attempts (
            attempt_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
            input_revision INTEGER NOT NULL,
            lease_generation INTEGER NOT NULL,
            attempt_no INTEGER NOT NULL,
            phase TEXT NOT NULL,
            status TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            catalog_hash TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            response_hash TEXT,
            draft_json TEXT,
            diagnostics_json TEXT NOT NULL DEFAULT '{}',
            usage_json TEXT NOT NULL DEFAULT '{}',
            latency_ms INTEGER,
            error_code TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            UNIQUE(run_id, lease_generation, attempt_no),
            CHECK(phase IN ('initial','repair')),
            CHECK(status IN ('requested','succeeded','failed')),
            CHECK(input_revision > 0),
            CHECK(lease_generation > 0),
            CHECK(attempt_no > 0)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_planner_attempts_run ON planner_attempts(run_id, lease_generation, attempt_no)"
    )


def _v10_allow_upload_identity_output_dedup(conn) -> None:
    """Permit a Task to expose an unchanged uploaded File as a dedup output.

    A content-addressed Artifact has one immutable metadata row.  A confirmed
    ``publish_file`` can legitimately return the identical bytes it consumed;
    creating a second row would violate CAS, while rejecting the Task would
    make an approved side effect non-executable.  A dedup output therefore
    remains limited to either its producer's Plan or an upload-origin Artifact
    that was explicitly materialized as the Task input.  Cross-Plan task
    output reuse remains forbidden.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_outputs'").fetchone():
        return
    conn.execute("DROP TRIGGER IF EXISTS task_outputs_owner_guard")
    conn.execute("DROP TRIGGER IF EXISTS task_outputs_owner_update_guard")
    guard = """CREATE TRIGGER {name} BEFORE {event} ON task_outputs
       WHEN (NEW.link_origin='producer' AND NOT EXISTS (
         SELECT 1 FROM artifacts WHERE artifact_id=NEW.artifact_id AND producer_task_id=NEW.task_id
       )) OR (NEW.link_origin='dedup' AND NOT EXISTS (
         SELECT 1
         FROM artifacts a
         WHERE a.artifact_id=NEW.artifact_id AND (
           a.origin='upload'
           OR EXISTS (
             SELECT 1 FROM tasks producer JOIN tasks consumer ON consumer.task_id=NEW.task_id
             WHERE producer.task_id=a.producer_task_id AND producer.plan_id=consumer.plan_id
           )
         )
       ))
       BEGIN SELECT RAISE(ABORT, 'task output producer must belong to task plan or be an upload identity output'); END"""
    conn.execute(guard.format(name="task_outputs_owner_guard", event="INSERT"))
    conn.execute(guard.format(name="task_outputs_owner_update_guard", event="UPDATE OF task_id,artifact_id,link_origin"))


def _v11_separate_artifact_identity_from_blob_hash(conn) -> None:
    """Allow several immutable semantic Artifacts to share one CAS blob.

    Runtime v1 used the SHA-256 digest as both the filesystem blob identity and
    the public Artifact ID.  That makes a File and a Vector with identical
    bytes indistinguishable, and permits their CRS/schema provenance to alias.
    Rebuild only the parent table: all child foreign keys still name
    ``artifacts(artifact_id)``, so the references remain intact while the old
    rows retain their historical IDs.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'").fetchone():
        return
    # Some early standalone stores contain only the artifact/lease fragment
    # used by the v3 repair test. They do not have the Runtime task graph, so
    # recreating a table with its required producer FK would make that legacy
    # repair database invalid. A complete Runtime database always has tasks.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone():
        return
    unique_hash = False
    for index in conn.execute("PRAGMA index_list(artifacts)").fetchall():
        if not index[2]:
            continue
        columns = [row[2] for row in conn.execute(f"PRAGMA index_info({index[1]})").fetchall()]
        if columns == ["content_hash"]:
            unique_hash = True
            break
    if not unique_hash:
        return
    conn.execute("PRAGMA defer_foreign_keys=ON")
    conn.execute("""CREATE TABLE artifacts_runtime_v11 (
        artifact_id TEXT PRIMARY KEY,
        origin TEXT NOT NULL,
        producer_task_id TEXT REFERENCES tasks(task_id) ON DELETE RESTRICT,
        content_hash TEXT NOT NULL,
        storage_uri TEXT NOT NULL,
        media_type TEXT,
        type TEXT NOT NULL DEFAULT 'Artifact',
        crs TEXT NOT NULL DEFAULT '{}',
        schema TEXT NOT NULL DEFAULT '{}',
        size_bytes INTEGER NOT NULL,
        validation_status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        CHECK(validation_status IN ('valid', 'staged', 'pending', 'invalid', 'quarantined')),
        CHECK(origin IN ('upload','task')),
        CHECK((origin = 'upload' AND producer_task_id IS NULL) OR
              (origin = 'task' AND producer_task_id IS NOT NULL))
    )""")
    conn.execute("""INSERT INTO artifacts_runtime_v11(
        artifact_id,origin,producer_task_id,content_hash,storage_uri,media_type,
        type,crs,schema,size_bytes,validation_status,created_at
    ) SELECT artifact_id,origin,producer_task_id,content_hash,storage_uri,media_type,
        type,crs,schema,size_bytes,validation_status,created_at FROM artifacts""")
    conn.execute("DROP TABLE artifacts")
    conn.execute("ALTER TABLE artifacts_runtime_v11 RENAME TO artifacts")


MIGRATIONS = {1: SCHEMA, 2: _v2_plan_cache_columns, 3: _v3_artifact_metadata_and_lease_ddl, 4: _v4_confirmation_action_hash, 5: _v5_task_claim_leases, 6: _v6_retry_schedule, 7: _v7_worker_fencing_and_executor_identity, 8: _v8_executor_identity_not_null, 9: _v9_planner_attempts, 10: _v10_allow_upload_identity_output_dedup, 11: _v11_separate_artifact_identity_from_blob_hash}


def migrate(db) -> None:
    db.initialize()
