"""Transactional Stage 6 persistence API backed by SQLite."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from buildforme.db import ExecutionDB, dumps, loads, new_id, row_to_dict
from buildforme.storage import utc_now_iso


class Stage6Store:
    def __init__(self, db_path: Path | str):
        self.db = ExecutionDB(db_path)

    # —— Runs ——
    def save_run(self, run: dict[str, Any]) -> dict[str, Any]:
        record = dict(run)
        rid = str(record.get("id") or new_id("run"))
        record["id"] = rid
        now = utc_now_iso()
        record.setdefault("created_at", now)
        record["updated_at"] = now
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT id FROM runs WHERE id=?", (rid,)).fetchone()
            cols = (
                rid,
                str(record.get("project_id") or ""),
                record.get("task_id"),
                record.get("packet_id"),
                str(record.get("provider_id") or ""),
                str(record.get("repository") or ""),
                record.get("repository_local_path"),
                record.get("baseline_ref"),
                record.get("baseline_commit"),
                record.get("requested_target_branch") or record.get("target_branch"),
                record.get("execution_branch"),
                record.get("operating_mode"),
                record.get("risk"),
                str(record.get("status") or "draft"),
                str(record.get("execution_mode") or record.get("mode") or "dry_run"),
                record.get("scope_fingerprint"),
                record.get("constitution_version"),
                record.get("constitution_hash"),
                record.get("constitution_lease_id"),
                record.get("constitution_lease_fingerprint"),
                record.get("task_lock_id"),
                dumps(record),
                record["created_at"],
                record["updated_at"],
                record.get("started_at"),
                record.get("finished_at"),
                record.get("idempotency_key"),
            )
            if existing:
                conn.execute(
                    """UPDATE runs SET project_id=?, task_id=?, packet_id=?, provider_id=?, repository=?,
                    repository_local_path=?, baseline_ref=?, baseline_commit=?, requested_target_branch=?,
                    execution_branch=?, operating_mode=?, risk=?, status=?, execution_mode=?,
                    scope_fingerprint=?, constitution_version=?, constitution_hash=?, constitution_lease_id=?,
                    constitution_lease_fingerprint=?, task_lock_id=?, payload_json=?, updated_at=?,
                    started_at=?, finished_at=?, idempotency_key=? WHERE id=?""",
                    (
                        str(record.get("project_id") or ""),
                        record.get("task_id"),
                        record.get("packet_id"),
                        str(record.get("provider_id") or ""),
                        str(record.get("repository") or ""),
                        record.get("repository_local_path"),
                        record.get("baseline_ref"),
                        record.get("baseline_commit"),
                        record.get("requested_target_branch") or record.get("target_branch"),
                        record.get("execution_branch"),
                        record.get("operating_mode"),
                        record.get("risk"),
                        str(record.get("status") or "draft"),
                        str(record.get("execution_mode") or record.get("mode") or "dry_run"),
                        record.get("scope_fingerprint"),
                        record.get("constitution_version"),
                        record.get("constitution_hash"),
                        record.get("constitution_lease_id"),
                        record.get("constitution_lease_fingerprint"),
                        record.get("task_lock_id"),
                        dumps(record),
                        record["updated_at"],
                        record.get("started_at"),
                        record.get("finished_at"),
                        record.get("idempotency_key"),
                        rid,
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO runs(
                    id, project_id, task_id, packet_id, provider_id, repository, repository_local_path,
                    baseline_ref, baseline_commit, requested_target_branch, execution_branch, operating_mode,
                    risk, status, execution_mode, scope_fingerprint, constitution_version, constitution_hash,
                    constitution_lease_id, constitution_lease_fingerprint, task_lock_id, payload_json,
                    created_at, updated_at, started_at, finished_at, idempotency_key
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    cols,
                )
        return record

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT payload_json FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(f"Run not found: {run_id}")
        return loads(row[0], {})

    def list_runs(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            if project_id:
                rows = conn.execute(
                    "SELECT payload_json FROM runs WHERE project_id=? ORDER BY created_at DESC",
                    (project_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT payload_json FROM runs ORDER BY created_at DESC").fetchall()
        return [loads(r[0], {}) for r in rows]

    def append_run_event(
        self,
        run_id: str,
        event_type: str,
        summary: str = "",
        *,
        actor: str = "system",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "id": new_id("re"),
            "run_id": run_id,
            "event_type": event_type,
            "summary": summary,
            "actor": actor,
            "metadata": metadata or {},
            "created_at": utc_now_iso(),
        }
        with self.db.transaction() as conn:
            # ensure run exists for FK — if missing, still store event via deferred? require run
            exists = conn.execute("SELECT id FROM runs WHERE id=?", (run_id,)).fetchone()
            if not exists:
                # create placeholder run shell for events during partial states
                conn.execute(
                    """INSERT OR IGNORE INTO runs(id, project_id, provider_id, repository, status, execution_mode, payload_json, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        "unknown",
                        "unknown",
                        "unknown/unknown",
                        "draft",
                        "dry_run",
                        dumps({"id": run_id, "status": "draft"}),
                        utc_now_iso(),
                        utc_now_iso(),
                    ),
                )
            conn.execute(
                """INSERT INTO run_events(id, run_id, event_type, summary, actor, metadata_json, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    event["id"],
                    run_id,
                    event_type,
                    summary,
                    actor,
                    dumps(metadata or {}),
                    event["created_at"],
                ),
            )
        return event

    def list_run_events(self, run_id: str) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT id, run_id, event_type, summary, actor, metadata_json, created_at FROM run_events WHERE run_id=? ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "id": r[0],
                    "run_id": r[1],
                    "event_type": r[2],
                    "type": r[2],
                    "summary": r[3],
                    "actor": r[4],
                    "metadata": loads(r[5], {}),
                    "created_at": r[6],
                }
            )
        return out

    # —— Approvals ——
    def save_run_approval(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = dict(payload)
        record.setdefault("id", new_id("rap"))
        now = utc_now_iso()
        record.setdefault("created_at", now)
        record["updated_at"] = now
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO run_approvals(id, run_id, requirement_type, decision, scope_fingerprint,
                   constitution_hash, constitution_lease_id, note, actor, payload_json, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id, requirement_type) DO UPDATE SET
                   decision=excluded.decision, scope_fingerprint=excluded.scope_fingerprint,
                   constitution_hash=excluded.constitution_hash, constitution_lease_id=excluded.constitution_lease_id,
                   note=excluded.note, actor=excluded.actor, payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
                (
                    record["id"],
                    record["run_id"],
                    record["requirement_type"],
                    record["decision"],
                    record.get("scope_fingerprint") or record.get("scope"),
                    record.get("constitution_hash"),
                    record.get("constitution_lease_id"),
                    record.get("note"),
                    record.get("actor"),
                    dumps(record),
                    record["created_at"],
                    record["updated_at"],
                ),
            )
        return record

    def list_run_approvals(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT payload_json FROM run_approvals WHERE run_id=?", (run_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT payload_json FROM run_approvals").fetchall()
        return [loads(r[0], {}) for r in rows]

    # —— Leases ——
    def save_constitution_lease(self, lease: dict[str, Any]) -> dict[str, Any]:
        from governance.constitution_lease import lease_records_equal, validate_lease_integrity

        problems = validate_lease_integrity(lease)
        if problems:
            raise ValueError("invalid constitution lease: " + "; ".join(problems))
        lid = str(lease["lease_id"])
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT payload_json FROM constitution_leases WHERE lease_id=?", (lid,)
            ).fetchone()
            if existing:
                prev = loads(existing[0], {})
                if not lease_records_equal(prev, lease):
                    raise ValueError(f"constitution lease mutation forbidden: lease_id={lid}")
                return prev
            record = dict(lease)
            record["stored_at"] = utc_now_iso()
            conn.execute(
                """INSERT INTO constitution_leases(lease_id, run_id, provider_id, packet_id,
                   constitution_version, constitution_hash, lease_fingerprint, payload_json, stored_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    lid,
                    lease.get("run_id"),
                    lease.get("provider_id"),
                    lease.get("packet_id"),
                    lease.get("constitution_version"),
                    lease.get("constitution_hash"),
                    lease.get("lease_fingerprint"),
                    dumps(record),
                    record["stored_at"],
                ),
            )
            return record

    def get_constitution_lease(self, lease_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM constitution_leases WHERE lease_id=?", (lease_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"Constitution lease not found: {lease_id}")
        return loads(row[0], {})

    def list_constitution_leases(self, *, limit: int = 100, run_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT payload_json FROM constitution_leases WHERE run_id=? ORDER BY stored_at DESC LIMIT ?",
                    (run_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM constitution_leases ORDER BY stored_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [loads(r[0], {}) for r in rows]

    # —— Locks ——
    def create_task_lock(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = {
            "id": str(payload.get("id") or new_id("tlock")),
            "task_key": str(payload.get("task_key") or "").strip(),
            "project_id": payload.get("project_id"),
            "run_id": payload.get("run_id"),
            "reason": str(payload.get("reason") or ""),
            "active": True,
            "created_at": utc_now_iso(),
            "released_at": None,
        }
        if not record["task_key"]:
            raise ValueError("task_key required")
        with self.db.transaction() as conn:
            try:
                conn.execute(
                    """INSERT INTO task_locks(id, task_key, project_id, run_id, reason, active, created_at, released_at)
                       VALUES (?,?,?,?,?,1,?,NULL)""",
                    (
                        record["id"],
                        record["task_key"],
                        record["project_id"],
                        record["run_id"],
                        record["reason"],
                        record["created_at"],
                    ),
                )
            except Exception as exc:
                raise ValueError(f"task lock already active: {record['task_key']}") from exc
        return record

    def release_task_lock(self, lock_id: str, *, reason: str = "") -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM task_locks WHERE id=?", (lock_id,)).fetchone()
            if not row:
                raise KeyError(f"Task lock not found: {lock_id}")
            conn.execute(
                "UPDATE task_locks SET active=0, released_at=?, release_reason=? WHERE id=?",
                (utc_now_iso(), reason, lock_id),
            )
        return {
            "id": lock_id,
            "active": False,
            "released_at": utc_now_iso(),
            "release_reason": reason,
        }

    def list_task_locks(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            if active_only:
                rows = conn.execute(
                    "SELECT id, task_key, project_id, run_id, reason, active, created_at, released_at FROM task_locks WHERE active=1"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, task_key, project_id, run_id, reason, active, created_at, released_at FROM task_locks"
                ).fetchall()
        return [
            {
                "id": r[0],
                "task_key": r[1],
                "project_id": r[2],
                "run_id": r[3],
                "reason": r[4],
                "active": bool(r[5]),
                "created_at": r[6],
                "released_at": r[7],
            }
            for r in rows
        ]

    def create_repository_lock(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = {
            "id": str(payload.get("id") or new_id("rlock")),
            "repository": str(payload.get("repository") or ""),
            "lock_scope": str(payload.get("lock_scope") or "all"),
            "reason": str(payload.get("reason") or ""),
            "project_id": payload.get("project_id"),
            "active": True,
            "created_at": utc_now_iso(),
        }
        with self.db.transaction() as conn:
            try:
                conn.execute(
                    """INSERT INTO repository_locks(id, repository, lock_scope, reason, project_id, active, created_at, payload_json)
                       VALUES (?,?,?,?,?,1,?,?)""",
                    (
                        record["id"],
                        record["repository"],
                        record["lock_scope"],
                        record["reason"],
                        record["project_id"],
                        record["created_at"],
                        dumps(record),
                    ),
                )
            except Exception as exc:
                raise ValueError(f"repository lock collision: {record['repository']}") from exc
        return record

    def release_repository_lock(self, lock_id: str, *, reason: str = "") -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT payload_json FROM repository_locks WHERE id=?", (lock_id,)).fetchone()
            if not row:
                raise KeyError(f"Repository lock not found: {lock_id}")
            conn.execute(
                "UPDATE repository_locks SET active=0, released_at=? WHERE id=?",
                (utc_now_iso(), lock_id),
            )
            data = loads(row[0], {})
            data["active"] = False
            data["released_at"] = utc_now_iso()
            return data

    def list_repository_locks(self, *, active_only: bool = False, repository: str | None = None) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            q = "SELECT payload_json, active, repository FROM repository_locks WHERE 1=1"
            params: list[Any] = []
            if active_only:
                q += " AND active=1"
            if repository:
                q += " AND repository=?"
                params.append(repository)
            rows = conn.execute(q, params).fetchall()
        out = []
        for r in rows:
            item = loads(r[0], {})
            item["active"] = bool(r[1])
            out.append(item)
        return out

    # —— Bindings ——
    def register_repository_binding(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo = str(payload.get("repository") or "").strip()
        path = str(payload.get("local_path") or "").strip()
        if not repo or not path:
            raise ValueError("repository and local_path required")
        now = utc_now_iso()
        record = {
            "id": str(payload.get("id") or new_id("rbind")),
            "repository": repo,
            "local_path": path,
            "project_id": payload.get("project_id"),
            "created_at": now,
            "updated_at": now,
        }
        with self.db.transaction() as conn:
            # path uniqueness across different repos
            clash = conn.execute(
                "SELECT repository FROM repository_bindings WHERE local_path=? AND lower(repository)!=lower(?)",
                (path, repo),
            ).fetchone()
            if clash:
                raise ValueError("local_path already bound to another repository")
            existing = conn.execute(
                "SELECT id, created_at FROM repository_bindings WHERE lower(repository)=lower(?)",
                (repo,),
            ).fetchone()
            if existing:
                record["id"] = existing[0]
                record["created_at"] = existing[1]
                conn.execute(
                    """UPDATE repository_bindings SET local_path=?, project_id=?, updated_at=? WHERE id=?""",
                    (path, record["project_id"], now, record["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO repository_bindings(id, repository, local_path, project_id, created_at, updated_at)
                       VALUES (?,?,?,?,?,?)""",
                    (record["id"], repo, path, record["project_id"], now, now),
                )
        return record

    def list_repository_bindings(self) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT id, repository, local_path, project_id, created_at, updated_at FROM repository_bindings"
            ).fetchall()
        return [
            {
                "id": r[0],
                "repository": r[1],
                "local_path": r[2],
                "project_id": r[3],
                "created_at": r[4],
                "updated_at": r[5],
            }
            for r in rows
        ]

    def get_repository_binding(self, repository: str) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id, repository, local_path, project_id, created_at, updated_at FROM repository_bindings WHERE lower(repository)=lower(?)",
                (repository,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "repository": row[1],
            "local_path": row[2],
            "project_id": row[3],
            "created_at": row[4],
            "updated_at": row[5],
        }

    # —— Evidence ——
    def save_run_evidence(self, evidence: dict[str, Any]) -> dict[str, Any]:
        record = dict(evidence)
        eid = str(record.get("evidence_id") or record.get("id") or new_id("ev"))
        record["evidence_id"] = eid
        record["id"] = eid
        record.setdefault("saved_at", utc_now_iso())
        record["immutable"] = True
        rid = str(record.get("run_id") or "")
        with self.db.transaction() as conn:
            exists = conn.execute("SELECT evidence_id FROM evidence WHERE evidence_id=?", (eid,)).fetchone()
            if exists:
                raise ValueError(f"evidence mutation forbidden: {eid} is append-only")
            prior = conn.execute(
                "SELECT COUNT(*) FROM evidence WHERE run_id=?", (rid,)
            ).fetchone()[0]
            record.setdefault("sequence", int(prior) + 1)
            record.setdefault("attempt", record.get("attempt") or record["sequence"])
            if prior:
                parent = conn.execute(
                    "SELECT evidence_id FROM evidence WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
                    (rid,),
                ).fetchone()
                if parent:
                    record.setdefault("parent_evidence_id", parent[0])
            # ensure run row exists for FK
            if rid and not conn.execute("SELECT id FROM runs WHERE id=?", (rid,)).fetchone():
                conn.execute(
                    """INSERT OR IGNORE INTO runs(id, project_id, provider_id, repository, status, execution_mode, payload_json, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        rid,
                        "unknown",
                        "unknown",
                        "unknown/unknown",
                        "draft",
                        "dry_run",
                        dumps({"id": rid}),
                        utc_now_iso(),
                        utc_now_iso(),
                    ),
                )
            conn.execute(
                """INSERT INTO evidence(evidence_id, run_id, sequence, attempt, parent_evidence_id,
                   payload_json, evidence_fingerprint, saved_at, immutable)
                   VALUES (?,?,?,?,?,?,?,?,1)""",
                (
                    eid,
                    rid,
                    record["sequence"],
                    record.get("attempt"),
                    record.get("parent_evidence_id"),
                    dumps(record),
                    record.get("evidence_fingerprint"),
                    record["saved_at"],
                ),
            )
        return record

    def get_run_evidence(self, run_id: str) -> dict[str, Any]:
        items = self.list_run_evidence(run_id=run_id, limit=1)
        if not items:
            raise KeyError(f"Evidence not found for run: {run_id}")
        return items[0]

    def list_run_evidence(self, *, run_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT payload_json FROM evidence WHERE run_id=? ORDER BY sequence DESC LIMIT ?",
                    (run_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM evidence ORDER BY saved_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [loads(r[0], {}) for r in rows]

    # —— Founder sessions ——
    def create_founder_session_record(self, record: dict[str, Any]) -> dict[str, Any]:
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO founder_sessions(token_hash, actor, csrf_token_hash, created_at, expires_at_epoch, revoked, active)
                   VALUES (?,?,?,?,?,?,1)""",
                (
                    record["token_hash"],
                    record["actor"],
                    record.get("csrf_token_hash"),
                    record["created_at"],
                    int(record["expires_at_epoch"]),
                    1 if record.get("revoked") else 0,
                ),
            )
        return record

    def validate_founder_token(self, token: str | None) -> dict[str, Any]:
        if not token:
            raise ValueError("founder authorization token required")
        digest = __import__("hashlib").sha256(str(token).encode("utf-8")).hexdigest()
        now = int(time.time())
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT actor, expires_at_epoch, revoked, active, csrf_token_hash FROM founder_sessions WHERE token_hash=?",
                (digest,),
            ).fetchone()
        if not row:
            raise ValueError("founder authorization token invalid")
        if row[2] or not row[3]:
            raise ValueError("founder session revoked")
        if int(row[1]) < now:
            raise ValueError("founder authorization token expired")
        return {"actor": row[0], "ok": True, "csrf_token_hash": row[4]}

    def revoke_founder_session(self, token: str) -> None:
        digest = __import__("hashlib").sha256(str(token).encode("utf-8")).hexdigest()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE founder_sessions SET revoked=1, active=0 WHERE token_hash=?",
                (digest,),
            )

    # —— Provider acks ——
    def set_provider_constitution_ack(self, provider_id: str, ack: dict[str, Any]) -> dict[str, Any]:
        record = dict(ack)
        record["provider_id"] = provider_id
        record["updated_at"] = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO provider_acks(provider_id, constitution_acknowledged, constitution_version,
                   constitution_hash, constitution_last_refresh, constitution_acknowledged_at, constitution_ack_actor,
                   payload_json, updated_at) VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(provider_id) DO UPDATE SET
                   constitution_acknowledged=excluded.constitution_acknowledged,
                   constitution_version=excluded.constitution_version,
                   constitution_hash=excluded.constitution_hash,
                   constitution_last_refresh=excluded.constitution_last_refresh,
                   constitution_acknowledged_at=excluded.constitution_acknowledged_at,
                   constitution_ack_actor=excluded.constitution_ack_actor,
                   payload_json=excluded.payload_json,
                   updated_at=excluded.updated_at""",
                (
                    provider_id,
                    1 if record.get("constitution_acknowledged") else 0,
                    record.get("constitution_version"),
                    record.get("constitution_hash"),
                    record.get("constitution_last_refresh"),
                    record.get("constitution_acknowledged_at"),
                    record.get("constitution_ack_actor"),
                    dumps(record),
                    record["updated_at"],
                ),
            )
        return record

    def get_provider_ack(self, provider_id: str) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM provider_acks WHERE provider_id=?", (provider_id,)
            ).fetchone()
        return loads(row[0], {}) if row else None

    # —— Execution control ——
    def get_execution_control(self) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT kill_switch_active, reason, actor, updated_at, payload_json FROM execution_control WHERE id=1"
            ).fetchone()
        if not row:
            return {"kill_switch_active": False, "reason": "", "actor": "system", "updated_at": utc_now_iso()}
        base = loads(row[4], {}) or {}
        base.update(
            {
                "kill_switch_active": bool(row[0]),
                "reason": row[1] or "",
                "actor": row[2] or "system",
                "updated_at": row[3],
            }
        )
        return base

    def set_execution_control(self, *, kill_switch_active: bool, reason: str = "", actor: str = "shan") -> dict[str, Any]:
        now = utc_now_iso()
        payload = {
            "kill_switch_active": bool(kill_switch_active),
            "reason": reason,
            "actor": actor,
            "updated_at": now,
        }
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO execution_control(id, kill_switch_active, reason, actor, updated_at, payload_json)
                   VALUES (1,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET kill_switch_active=excluded.kill_switch_active,
                   reason=excluded.reason, actor=excluded.actor, updated_at=excluded.updated_at,
                   payload_json=excluded.payload_json""",
                (1 if kill_switch_active else 0, reason, actor, now, dumps(payload)),
            )
        return payload

    def migrate_from_json(self, runtime_dir: Path) -> dict[str, Any]:
        """Idempotent import from legacy JSON runtime files with backup."""
        import json
        import shutil

        runtime_dir = Path(runtime_dir)
        backup = runtime_dir / f"json_backup_{utc_now_iso().replace(':', '')}"
        report = {"backup": str(backup), "imported": {}, "errors": []}
        if runtime_dir.exists():
            backup.mkdir(parents=True, exist_ok=True)
            for name in (
                "runs.json",
                "run_events.json",
                "run_approvals.json",
                "constitution_leases.json",
                "task_locks.json",
                "repository_locks.json",
                "repository_bindings.json",
                "run_evidence.json",
                "execution_control.json",
                "providers.json",
            ):
                src = runtime_dir / name
                if src.exists():
                    shutil.copy2(src, backup / name)
        # Import runs
        runs_path = runtime_dir / "runs.json"
        if runs_path.exists():
            try:
                data = json.loads(runs_path.read_text(encoding="utf-8") or "{}")
                count = 0
                for run in data.get("runs") or []:
                    if isinstance(run, dict) and run.get("id"):
                        try:
                            self.save_run(run)
                            count += 1
                        except Exception as exc:
                            report["errors"].append(f"run {run.get('id')}: {exc}")
                report["imported"]["runs"] = count
            except Exception as exc:
                report["errors"].append(f"runs.json: {exc}")
        # Leases
        leases_path = runtime_dir / "constitution_leases.json"
        if leases_path.exists():
            try:
                data = json.loads(leases_path.read_text(encoding="utf-8") or "{}")
                count = 0
                for lease in data.get("leases") or []:
                    if isinstance(lease, dict) and lease.get("lease_id"):
                        try:
                            self.save_constitution_lease(lease)
                            count += 1
                        except Exception as exc:
                            report["errors"].append(f"lease: {exc}")
                report["imported"]["leases"] = count
            except Exception as exc:
                report["errors"].append(f"leases: {exc}")
        # Bindings
        bind_path = runtime_dir / "repository_bindings.json"
        if bind_path.exists():
            try:
                data = json.loads(bind_path.read_text(encoding="utf-8") or "{}")
                count = 0
                for b in data.get("bindings") or []:
                    if isinstance(b, dict):
                        try:
                            self.register_repository_binding(b)
                            count += 1
                        except Exception as exc:
                            report["errors"].append(f"binding: {exc}")
                report["imported"]["bindings"] = count
            except Exception as exc:
                report["errors"].append(f"bindings: {exc}")
        report["db"] = self.db.pragmas()
        return report
