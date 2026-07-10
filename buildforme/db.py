"""SQLite transactional authority for Stage 6 execution state.

WAL mode, foreign keys, schema versioning, atomic locks.
JSON remains import/export only — not concurrency authority for live execution.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from buildforme.storage import utc_now_iso

SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  task_id TEXT,
  packet_id TEXT,
  provider_id TEXT NOT NULL,
  repository TEXT NOT NULL,
  repository_local_path TEXT,
  baseline_ref TEXT,
  baseline_commit TEXT,
  requested_target_branch TEXT,
  execution_branch TEXT,
  operating_mode TEXT,
  risk TEXT,
  status TEXT NOT NULL,
  execution_mode TEXT NOT NULL DEFAULT 'dry_run',
  scope_fingerprint TEXT,
  constitution_version TEXT,
  constitution_hash TEXT,
  constitution_lease_id TEXT,
  constitution_lease_fingerprint TEXT,
  task_lock_id TEXT,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  idempotency_key TEXT UNIQUE,
  row_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS run_events (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  event_type TEXT NOT NULL,
  summary TEXT,
  actor TEXT,
  metadata_json TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, created_at);

CREATE TABLE IF NOT EXISTS run_approvals (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  requirement_type TEXT NOT NULL,
  decision TEXT NOT NULL,
  scope_fingerprint TEXT,
  constitution_hash TEXT,
  constitution_lease_id TEXT,
  note TEXT,
  actor TEXT,
  payload_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(run_id, requirement_type)
);

CREATE TABLE IF NOT EXISTS constitution_leases (
  lease_id TEXT PRIMARY KEY,
  run_id TEXT,
  provider_id TEXT,
  packet_id TEXT,
  constitution_version TEXT NOT NULL,
  constitution_hash TEXT NOT NULL,
  lease_fingerprint TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  stored_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_locks (
  id TEXT PRIMARY KEY,
  task_key TEXT NOT NULL,
  project_id TEXT,
  run_id TEXT,
  reason TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  released_at TEXT,
  release_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_locks_active
  ON task_locks(project_id, task_key) WHERE active = 1;

CREATE TABLE IF NOT EXISTS repository_locks (
  id TEXT PRIMARY KEY,
  repository TEXT NOT NULL,
  lock_scope TEXT NOT NULL,
  reason TEXT,
  project_id TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  released_at TEXT,
  payload_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_repo_locks_write_active
  ON repository_locks(repository) WHERE active = 1 AND lock_scope IN ('all','write');

CREATE TABLE IF NOT EXISTS repository_bindings (
  id TEXT PRIMARY KEY,
  repository TEXT NOT NULL UNIQUE,
  local_path TEXT NOT NULL,
  project_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
  evidence_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  sequence INTEGER NOT NULL,
  attempt INTEGER,
  parent_evidence_id TEXT,
  payload_json TEXT NOT NULL,
  evidence_fingerprint TEXT,
  saved_at TEXT NOT NULL,
  immutable INTEGER NOT NULL DEFAULT 1,
  UNIQUE(run_id, sequence)
);

CREATE TABLE IF NOT EXISTS provider_acks (
  provider_id TEXT PRIMARY KEY,
  constitution_acknowledged INTEGER NOT NULL DEFAULT 0,
  constitution_version TEXT,
  constitution_hash TEXT,
  constitution_last_refresh TEXT,
  constitution_acknowledged_at TEXT,
  constitution_ack_actor TEXT,
  payload_json TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS founder_sessions (
  token_hash TEXT PRIMARY KEY,
  actor TEXT NOT NULL,
  csrf_token_hash TEXT,
  created_at TEXT NOT NULL,
  expires_at_epoch INTEGER NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS execution_control (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  kill_switch_active INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  actor TEXT,
  updated_at TEXT NOT NULL,
  payload_json TEXT
);

CREATE TABLE IF NOT EXISTS execution_policies (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  payload_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id TEXT PRIMARY KEY,
  action TEXT NOT NULL,
  entity_type TEXT,
  entity_id TEXT,
  detail TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_execution_controls (
  project_id TEXT PRIMARY KEY,
  execution_status TEXT NOT NULL,
  reason TEXT,
  actor TEXT,
  updated_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_compat_cache (
  provider_id TEXT PRIMARY KEY,
  executable TEXT,
  version_text TEXT,
  profile_json TEXT NOT NULL,
  live_ready INTEGER NOT NULL DEFAULT 0,
  checked_at TEXT NOT NULL,
  expires_at_epoch INTEGER NOT NULL
);
"""


class ExecutionDB:
    """Thread-safe SQLite connection manager for Stage 6.

    Uses short-lived connections per transaction so Windows file locks release
    promptly (important for tests and multi-instance coordination via SQLite).
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialized = False
        self.ensure_schema()

    def _raw_connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def ensure_schema(self) -> None:
        with self._lock:
            conn = self._raw_connect()
            try:
                conn.executescript(SCHEMA_SQL)
                row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
                if not row:
                    conn.execute(
                        "INSERT INTO schema_meta(key, value) VALUES ('version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO execution_control(id, kill_switch_active, reason, actor, updated_at, payload_json) VALUES (1, 0, '', 'system', ?, '{}')",
                        (utc_now_iso(),),
                    )
                else:
                    current = int(row[0] or 0)
                    if current < 2:
                        self._migrate_to_v2(conn)
                        conn.execute(
                            "UPDATE schema_meta SET value=? WHERE key='version'",
                            (str(SCHEMA_VERSION),),
                        )
                    elif current < SCHEMA_VERSION:
                        conn.execute(
                            "UPDATE schema_meta SET value=? WHERE key='version'",
                            (str(SCHEMA_VERSION),),
                        )
                conn.commit()
                self._initialized = True
            finally:
                conn.close()

    def _migrate_to_v2(self, conn: sqlite3.Connection) -> None:
        """Additive migration: row_version, project controls, compat cache."""
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        if "row_version" not in cols:
            conn.execute(
                "ALTER TABLE runs ADD COLUMN row_version INTEGER NOT NULL DEFAULT 1"
            )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS project_execution_controls (
              project_id TEXT PRIMARY KEY,
              execution_status TEXT NOT NULL,
              reason TEXT,
              actor TEXT,
              updated_at TEXT NOT NULL,
              payload_json TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS provider_compat_cache (
              provider_id TEXT PRIMARY KEY,
              executable TEXT,
              version_text TEXT,
              profile_json TEXT NOT NULL,
              live_ready INTEGER NOT NULL DEFAULT 0,
              checked_at TEXT NOT NULL,
              expires_at_epoch INTEGER NOT NULL
            )"""
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('migration_cutover', '')"
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._raw_connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def pragmas(self) -> dict[str, Any]:
        with self._lock:
            conn = self._raw_connect()
            try:
                journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
                fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                version = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
                return {
                    "journal_mode": journal,
                    "foreign_keys": bool(fk),
                    "integrity_check": integrity,
                    "schema_version": int(version[0]) if version else None,
                    "path": str(self.path),
                }
            finally:
                conn.close()

    def close(self) -> None:
        # Connections are short-lived; nothing persistent to close.
        return


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"
