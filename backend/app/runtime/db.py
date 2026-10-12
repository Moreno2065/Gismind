"""SQLite connection and migration primitives for the durable runtime."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


RUN_STATUSES = frozenset({"queued", "planning", "awaiting_input", "ready", "running", "cancelling", "completed", "failed", "cancelled"})
TASK_STATUSES = frozenset({"blocked", "ready", "leased", "running", "validating", "retry_wait", "succeeded", "failed", "cancelled"})
TASK_ATTEMPT_STATUSES = frozenset({"queued", "leased", "running", "validating", "succeeded", "failed", "cancelled", "retry_wait"})


def utc_now() -> str:
    """Return an RFC3339 timestamp normalized to UTC."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    message_index INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(session_id, message_index)
);
CREATE TABLE IF NOT EXISTS message_artifacts (
    message_id TEXT NOT NULL REFERENCES messages(message_id) ON DELETE RESTRICT,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    PRIMARY KEY(message_id, artifact_id)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
    run_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'queued',
    last_event_seq INTEGER NOT NULL DEFAULT 0,
    active_plan_id TEXT,
    planner_lease_owner TEXT,
    planner_lease_token TEXT,
    planner_lease_expires_at TEXT,
    planner_generation INTEGER NOT NULL DEFAULT 0,
    input_revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(status IN ('queued','planning','awaiting_input','ready','running','cancelling','completed','failed','cancelled')),
    FOREIGN KEY(active_plan_id) REFERENCES plans(plan_id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS run_input_revisions (
    revision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL,
    input_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, revision)
);
CREATE TABLE IF NOT EXISTS run_commands (
    command_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    command_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    applied_at TEXT,
    UNIQUE(run_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    plan_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'draft',
    plan_json TEXT NOT NULL DEFAULT '{}',
    input_revision_id TEXT,
    ir_version TEXT,
    compiler_version TEXT,
    plan_hash TEXT,
    ir_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, plan_version)
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id) ON DELETE RESTRICT,
    task_key TEXT NOT NULL,
    agent_role TEXT,
    tool_name TEXT,
    status TEXT NOT NULL DEFAULT 'ready',
    lease_generation INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    blocked_reason TEXT,
    task_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(status IN ('blocked','ready','leased','running','validating','retry_wait','succeeded','failed','cancelled')),
    UNIQUE(plan_id, task_key)
);
CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    PRIMARY KEY(task_id, depends_on_task_id)
);
CREATE TABLE IF NOT EXISTS task_input_bindings (
    binding_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    input_name TEXT NOT NULL,
    source_task_id TEXT REFERENCES tasks(task_id) ON DELETE RESTRICT,
    source_artifact_id TEXT REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    required INTEGER NOT NULL DEFAULT 1,
    ordinal INTEGER NOT NULL DEFAULT 0,
    source_port TEXT,
    source_ordinal INTEGER,
    source_all INTEGER NOT NULL DEFAULT 0,
    CHECK(ordinal >= 0),
    CHECK(source_ordinal IS NULL OR source_ordinal >= 0),
    CHECK(source_all IN (0,1)),
    CHECK(source_all = 0 OR source_ordinal IS NULL),
    CHECK(source_task_id IS NULL OR source_all = 1 OR source_ordinal IS NOT NULL),
    CHECK(source_task_id IS NULL OR source_port IS NOT NULL),
    CHECK(source_artifact_id IS NULL OR (source_port IS NULL AND source_ordinal IS NULL AND source_all = 0)),
    CHECK((source_task_id IS NULL) != (source_artifact_id IS NULL)),
    UNIQUE(task_id, input_name, ordinal)
);
CREATE TABLE IF NOT EXISTS task_confirmations (
    confirmation_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'pending',
    action_hash TEXT,
    confirmed_by TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    CHECK(status IN ('pending','confirmed','rejected','cancelled'))
);
CREATE TABLE IF NOT EXISTS task_attempts (
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
);
CREATE TABLE IF NOT EXISTS task_inputs (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    input_name TEXT NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL DEFAULT 0,
    CHECK(ordinal >= 0),
    PRIMARY KEY(task_id, input_name, ordinal)
);
CREATE TABLE IF NOT EXISTS task_outputs (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    output_name TEXT NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    link_origin TEXT NOT NULL DEFAULT 'producer',
    CHECK(ordinal >= 0),
    CHECK(link_origin IN ('producer','dedup')),
    PRIMARY KEY(task_id, output_name, ordinal)
);
CREATE TABLE IF NOT EXISTS artifacts (
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
);
CREATE TABLE IF NOT EXISTS artifact_lineage (
    lineage_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    parent_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    relation TEXT NOT NULL DEFAULT 'derived_from',
    created_at TEXT NOT NULL,
    UNIQUE(artifact_id, parent_artifact_id, relation)
);
CREATE TABLE IF NOT EXISTS artifact_pins (
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    pin_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(artifact_id, pin_key)
);
CREATE TABLE IF NOT EXISTS artifact_leases (
    artifact_id TEXT REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    content_hash TEXT NOT NULL,
    lease_key TEXT NOT NULL,
    purpose TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(lease_key),
    CHECK(artifact_id IS NOT NULL OR content_hash IS NOT NULL),
    CHECK(purpose IN ('commit','download','backup','gc'))
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(run_id, seq)
);
CREATE TABLE IF NOT EXISTS cache_entries (
    cache_key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
    ,contract_version TEXT
    ,execution_fingerprint TEXT
    ,input_fingerprint TEXT
);
CREATE TABLE IF NOT EXISTS cache_artifacts (
    cache_key TEXT NOT NULL REFERENCES cache_entries(cache_key) ON DELETE RESTRICT,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    PRIMARY KEY(cache_key, artifact_id)
);
"""

SCHEMA_STATEMENTS = tuple(statement.strip() for statement in SCHEMA.split(";") if statement.strip())

REFERENCE_GUARD_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS artifact_origin_guard BEFORE INSERT ON artifacts
       WHEN NEW.origin IS NULL OR NEW.origin NOT IN ('upload','task')
       BEGIN SELECT RAISE(ABORT, 'artifact origin must be upload or task'); END""",
    """CREATE TRIGGER IF NOT EXISTS artifact_origin_update_guard BEFORE UPDATE OF origin ON artifacts
       WHEN NEW.origin IS NULL OR NEW.origin NOT IN ('upload','task')
       BEGIN SELECT RAISE(ABORT, 'artifact origin must be upload or task'); END""",
    """CREATE TRIGGER IF NOT EXISTS valid_artifact_metadata_guard BEFORE INSERT ON artifacts
       WHEN NEW.validation_status='valid' AND NOT (
         length(NEW.content_hash)=64 AND lower(NEW.content_hash)=NEW.content_hash
         AND NEW.content_hash NOT GLOB '*[^0-9a-f]*' AND NEW.size_bytes >= 0
         AND NEW.storage_uri='artifacts/sha256/' || substr(NEW.content_hash,1,2) || '/' || NEW.content_hash)
       BEGIN SELECT RAISE(ABORT, 'valid artifacts require canonical CAS metadata'); END""",
    """CREATE TRIGGER IF NOT EXISTS valid_artifact_metadata_update_guard BEFORE UPDATE OF validation_status,storage_uri,content_hash,size_bytes ON artifacts
       WHEN NEW.validation_status='valid' AND NOT (
         length(NEW.content_hash)=64 AND lower(NEW.content_hash)=NEW.content_hash
         AND NEW.content_hash NOT GLOB '*[^0-9a-f]*' AND NEW.size_bytes >= 0
         AND NEW.storage_uri='artifacts/sha256/' || substr(NEW.content_hash,1,2) || '/' || NEW.content_hash)
       BEGIN SELECT RAISE(ABORT, 'valid artifacts require canonical CAS metadata'); END""",
    """CREATE TRIGGER IF NOT EXISTS task_inputs_valid_artifact BEFORE INSERT ON task_inputs
       WHEN NOT EXISTS (SELECT 1 FROM artifacts WHERE artifact_id=NEW.artifact_id AND validation_status='valid'
                        AND storage_uri='artifacts/sha256/' || substr(content_hash,1,2) || '/' || content_hash)
       BEGIN SELECT RAISE(ABORT, 'only valid artifacts may be task inputs'); END""",
    """CREATE TRIGGER IF NOT EXISTS task_inputs_valid_artifact_update BEFORE UPDATE OF artifact_id ON task_inputs
       WHEN NOT EXISTS (SELECT 1 FROM artifacts WHERE artifact_id=NEW.artifact_id AND validation_status='valid'
                        AND storage_uri='artifacts/sha256/' || substr(content_hash,1,2) || '/' || content_hash)
       BEGIN SELECT RAISE(ABORT, 'only valid artifacts may be task inputs'); END""",
    """CREATE TRIGGER IF NOT EXISTS task_outputs_valid_artifact BEFORE INSERT ON task_outputs
       WHEN NOT EXISTS (SELECT 1 FROM artifacts WHERE artifact_id=NEW.artifact_id AND validation_status='valid'
                        AND storage_uri='artifacts/sha256/' || substr(content_hash,1,2) || '/' || content_hash)
       BEGIN SELECT RAISE(ABORT, 'only valid artifacts may be task outputs'); END""",
    """CREATE TRIGGER IF NOT EXISTS task_outputs_owner_guard BEFORE INSERT ON task_outputs
       WHEN NOT EXISTS (SELECT 1 FROM artifacts WHERE artifact_id=NEW.artifact_id AND producer_task_id=NEW.task_id)
       BEGIN SELECT RAISE(ABORT, 'task output producer must match task'); END""",
    """CREATE TRIGGER IF NOT EXISTS task_outputs_valid_artifact_update BEFORE UPDATE OF artifact_id ON task_outputs
       WHEN NOT EXISTS (SELECT 1 FROM artifacts WHERE artifact_id=NEW.artifact_id AND validation_status='valid'
                        AND storage_uri='artifacts/sha256/' || substr(content_hash,1,2) || '/' || content_hash)
       BEGIN SELECT RAISE(ABORT, 'only valid artifacts may be task outputs'); END""",
    """CREATE TRIGGER IF NOT EXISTS task_outputs_owner_update_guard BEFORE UPDATE OF task_id,artifact_id ON task_outputs
       WHEN NOT EXISTS (SELECT 1 FROM artifacts WHERE artifact_id=NEW.artifact_id AND producer_task_id=NEW.task_id)
       BEGIN SELECT RAISE(ABORT, 'task output producer must match task'); END""",
    """CREATE TRIGGER IF NOT EXISTS message_artifacts_valid_artifact BEFORE INSERT ON message_artifacts
       WHEN NOT EXISTS (SELECT 1 FROM artifacts WHERE artifact_id=NEW.artifact_id AND validation_status='valid'
                        AND storage_uri='artifacts/sha256/' || substr(content_hash,1,2) || '/' || content_hash)
       BEGIN SELECT RAISE(ABORT, 'only valid artifacts may be message references'); END""",
    """CREATE TRIGGER IF NOT EXISTS message_artifacts_valid_artifact_update BEFORE UPDATE OF artifact_id ON message_artifacts
       WHEN NOT EXISTS (SELECT 1 FROM artifacts WHERE artifact_id=NEW.artifact_id AND validation_status='valid'
                        AND storage_uri='artifacts/sha256/' || substr(content_hash,1,2) || '/' || content_hash)
       BEGIN SELECT RAISE(ABORT, 'only valid artifacts may be message references'); END""",
    """CREATE TRIGGER IF NOT EXISTS artifact_reference_integrity_guard BEFORE UPDATE OF validation_status,storage_uri,content_hash ON artifacts
       WHEN EXISTS (SELECT 1 FROM task_inputs WHERE artifact_id=NEW.artifact_id)
          OR EXISTS (SELECT 1 FROM task_outputs WHERE artifact_id=NEW.artifact_id)
          OR EXISTS (SELECT 1 FROM message_artifacts WHERE artifact_id=NEW.artifact_id)
       BEGIN
         SELECT CASE WHEN NOT (NEW.validation_status='valid'
           AND NEW.storage_uri='artifacts/sha256/' || substr(NEW.content_hash,1,2) || '/' || NEW.content_hash)
           THEN RAISE(ABORT, 'referenced artifact must remain valid canonical CAS') END;
       END""",
)

RUNTIME_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_artifacts_content_hash ON artifacts(content_hash)",
    "CREATE UNIQUE INDEX IF NOT EXISTS one_active_task_attempt ON task_attempts(task_id) WHERE status IN ('queued','leased','running','validating','retry_wait')",
    "CREATE UNIQUE INDEX IF NOT EXISTS one_pending_task_confirmation ON task_confirmations(task_id) WHERE status='pending'",
    "CREATE INDEX IF NOT EXISTS idx_artifact_leases_content_hash ON artifact_leases(content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_artifact_lineage_parent ON artifact_lineage(parent_artifact_id)",
    "CREATE INDEX IF NOT EXISTS idx_artifact_pins_artifact ON artifact_pins(artifact_id)",
    "CREATE INDEX IF NOT EXISTS idx_cache_artifacts_artifact ON cache_artifacts(artifact_id)",
)


class RuntimeDB:
    """Own SQLite connections and apply the stage-one schema."""

    def __init__(self, path: str | Path | None = None, *, migrations=None):
        if path is None:
            try:
                from app.config import get_settings
                path = get_settings().runtime_paths["runtime_db"]
            except (ImportError, KeyError, ValueError) as exc:
                raise ValueError("RuntimeDB requires resolved runtime configuration (RuntimePaths) or an explicit path") from exc
        self.path = Path(path)
        self._migrations = migrations
        self._local = threading.local()
        self._memory_uri = None
        self._memory_anchor = None
        if str(path) == ":memory:":
            self._memory_uri = f"file:gismind_runtime_{id(self)}?mode=memory&cache=shared"
            self._memory_anchor = self._connect()

    def _connect(self) -> sqlite3.Connection:
        if self._memory_uri:
            conn = sqlite3.connect(self._memory_uri, uri=True, timeout=30, isolation_level=None)
        else:
            if str(self.path) != ":memory:":
                self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def initialize(self) -> None:
        with self.transaction() as conn:
            # Do not use executescript here: sqlite3 commits implicitly before
            # executescript, which would make a failed migration partial.
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            versions = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            if self._migrations is None:
                from .migrations import MIGRATIONS
                migration_map = MIGRATIONS
            else:
                migration_map = self._migrations
            migration_map = dict(migration_map)
            known = sorted(migration_map)
            if known != list(range(1, len(known) + 1)):
                raise RuntimeError("migration versions must be contiguous starting at 1")
            if any(version not in migration_map for version in versions):
                raise RuntimeError(f"unknown migration version(s): {sorted(set(versions) - set(migration_map))}")
            if versions and versions != set(range(1, max(versions) + 1)):
                raise RuntimeError("applied migration versions must be contiguous starting at 1")
            current = max(versions, default=0)
            for version in known:
                if version <= current:
                    continue
                payload = migration_map[version]
                if callable(payload):
                    payload(conn)
                else:
                    statements = payload if isinstance(payload, (tuple, list)) else str(payload).split(";")
                    for statement in statements:
                        if str(statement).strip():
                            conn.execute(str(statement).strip())
                conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (version, utc_now()))
            for statement in REFERENCE_GUARD_TRIGGERS:
                target = statement.split(" ON ", 1)[1].split()[0]
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (target,)).fetchone():
                    conn.execute(statement)
            for statement in RUNTIME_INDEX_STATEMENTS:
                target = statement.split(" ON ", 1)[1].split("(", 1)[0]
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (target,)).fetchone():
                    conn.execute(statement)

    @staticmethod
    def _upgrade_artifact_lease_hash_constraint(conn: sqlite3.Connection) -> None:
        """Safely upgrade pre-stage-one nullable lease hashes in-place."""
        info = conn.execute("PRAGMA table_info(artifact_leases)").fetchall()
        hash_info = next((row for row in info if row[1] == "content_hash"), None)
        if hash_info is None or hash_info[3]:
            return
        conn.execute(
            "UPDATE artifact_leases SET content_hash=(SELECT content_hash FROM artifacts WHERE artifacts.artifact_id=artifact_leases.artifact_id) WHERE content_hash IS NULL AND artifact_id IS NOT NULL"
        )

    @staticmethod
    def _upgrade_artifact_metadata_columns(conn: sqlite3.Connection) -> None:
        """Add the typed artifact metadata columns to pre-runtime databases."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(artifacts)")}
        additions = (
            ("type", "TEXT NOT NULL DEFAULT 'Artifact'"),
            ("crs", "TEXT NOT NULL DEFAULT '{}'"),
            ("schema", "TEXT NOT NULL DEFAULT '{}'"),
        )
        added = False
        for name, definition in additions:
            if name not in columns:
                conn.execute(f"ALTER TABLE artifacts ADD COLUMN {name} {definition}")
                added = True
        if added:
            # Legacy rows were never checked against the typed metadata and
            # must not silently remain publishable as valid CAS objects.
            conn.execute("UPDATE artifacts SET validation_status='quarantined' WHERE validation_status='valid'")
        elif {"type", "crs", "schema"}.issubset(columns):
            conn.execute("UPDATE artifacts SET validation_status='quarantined' WHERE validation_status='valid' AND (type IS NULL OR type='' OR crs IS NULL OR schema IS NULL)")
        if conn.execute("SELECT 1 FROM artifact_leases WHERE content_hash IS NULL LIMIT 1").fetchone():
            raise RuntimeError("cannot upgrade artifact leases with unresolved content hashes")
        conn.execute("ALTER TABLE artifact_leases RENAME TO artifact_leases_legacy")
        conn.execute("""
            CREATE TABLE artifact_leases (
                artifact_id TEXT REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
                content_hash TEXT NOT NULL,
                lease_key TEXT NOT NULL,
                purpose TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(lease_key),
                CHECK(artifact_id IS NOT NULL OR content_hash IS NOT NULL),
                CHECK(purpose IN ('commit','download','backup','gc'))
            )
        """)
        conn.execute("INSERT INTO artifact_leases(artifact_id,content_hash,lease_key,purpose,expires_at,created_at) SELECT artifact_id,content_hash,lease_key,purpose,expires_at,created_at FROM artifact_leases_legacy")
        conn.execute("DROP TABLE artifact_leases_legacy")

    @staticmethod
    def _upgrade_plan_cache_columns(conn: sqlite3.Connection) -> None:
        """Add PlanIR/cache identity columns without rebuilding existing tables."""
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

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a standalone autocommit connection for read/write operations."""
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            yield existing
            return
        conn = self._connect()
        self._local.connection = conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._local.connection = None
            conn.close()

    @property
    def active_connection(self) -> sqlite3.Connection | None:
        return getattr(self._local, "connection", None)

    def close(self) -> None:
        if self._memory_anchor is not None:
            self._memory_anchor.close()
            self._memory_anchor = None


RuntimeDatabase = RuntimeDB
