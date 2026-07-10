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
    def save_run(self, run: dict[str, Any], *, expected_row_version: int | None = None) -> dict[str, Any]:
        """Persist a run. When expected_row_version is set, reject stale writers."""
        record = dict(run)
        rid = str(record.get("id") or new_id("run"))
        record["id"] = rid
        now = utc_now_iso()
        record.setdefault("created_at", now)
        record["updated_at"] = now
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT id, row_version FROM runs WHERE id=?", (rid,)
            ).fetchone()
            if existing:
                current_ver = int(existing[1] or 1)
                if expected_row_version is not None and current_ver != int(expected_row_version):
                    raise ValueError(
                        f"stale run write rejected: expected row_version={expected_row_version} "
                        f"have={current_ver} run_id={rid}"
                    )
                new_ver = current_ver + 1
                record["row_version"] = new_ver
                cur = conn.execute(
                    """UPDATE runs SET project_id=?, task_id=?, packet_id=?, provider_id=?, repository=?,
                    repository_local_path=?, baseline_ref=?, baseline_commit=?, requested_target_branch=?,
                    execution_branch=?, operating_mode=?, risk=?, status=?, execution_mode=?,
                    scope_fingerprint=?, constitution_version=?, constitution_hash=?, constitution_lease_id=?,
                    constitution_lease_fingerprint=?, task_lock_id=?, payload_json=?, updated_at=?,
                    started_at=?, finished_at=?, idempotency_key=?, row_version=? WHERE id=? AND row_version=?""",
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
                        new_ver,
                        rid,
                        current_ver,
                    ),
                )
                if cur.rowcount == 0:
                    raise ValueError(f"stale run write race: run_id={rid}")
            else:
                record.setdefault("row_version", 1)
                conn.execute(
                    """INSERT INTO runs(
                    id, project_id, task_id, packet_id, provider_id, repository, repository_local_path,
                    baseline_ref, baseline_commit, requested_target_branch, execution_branch, operating_mode,
                    risk, status, execution_mode, scope_fingerprint, constitution_version, constitution_hash,
                    constitution_lease_id, constitution_lease_fingerprint, task_lock_id, payload_json,
                    created_at, updated_at, started_at, finished_at, idempotency_key, row_version
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
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
                        int(record.get("row_version") or 1),
                    ),
                )
        return record

    def admit_run_atomic(
        self,
        *,
        run: dict[str, Any],
        lease: dict[str, Any] | None = None,
        task_lock: dict[str, Any] | None = None,
        event_type: str = "run_created",
        event_summary: str = "Draft supervised run created",
        event_actor: str = "system",
        event_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomic Stage 6 admission: optional lock + lease + run + initial event.

        All records commit together or roll back together. Idempotency: if
        idempotency_key already maps to a run, return that run without mutation.
        """
        from governance.constitution_lease import lease_records_equal, validate_lease_integrity

        record = dict(run)
        rid = str(record.get("id") or new_id("run"))
        record["id"] = rid
        now = utc_now_iso()
        record.setdefault("created_at", now)
        record["updated_at"] = now
        record.setdefault("row_version", 1)
        idemp = record.get("idempotency_key")

        with self.db.transaction() as conn:
            if idemp:
                hit = conn.execute(
                    "SELECT payload_json FROM runs WHERE idempotency_key=?", (str(idemp),)
                ).fetchone()
                if hit:
                    return loads(hit[0], {})

            existing = conn.execute("SELECT id FROM runs WHERE id=?", (rid,)).fetchone()
            if existing:
                raise ValueError(f"run already admitted: {rid}")

            lock_id = None
            if task_lock:
                lock_id = str(task_lock.get("id") or new_id("tlock"))
                task_key = str(task_lock.get("task_key") or "").strip()
                if not task_key:
                    raise ValueError("task_key required for task lock")
                try:
                    conn.execute(
                        """INSERT INTO task_locks(id, task_key, project_id, run_id, reason, active, created_at, released_at)
                           VALUES (?,?,?,?,?,1,?,NULL)""",
                        (
                            lock_id,
                            task_key,
                            task_lock.get("project_id"),
                            rid,
                            str(task_lock.get("reason") or ""),
                            now,
                        ),
                    )
                except Exception as exc:
                    raise ValueError(f"task lock already active: {task_key}") from exc
                record["task_lock_id"] = lock_id

            if lease:
                problems = validate_lease_integrity(lease)
                if problems:
                    raise ValueError("invalid constitution lease: " + "; ".join(problems))
                lid = str(lease["lease_id"])
                prev = conn.execute(
                    "SELECT payload_json FROM constitution_leases WHERE lease_id=?", (lid,)
                ).fetchone()
                if prev:
                    if not lease_records_equal(loads(prev[0], {}), lease):
                        raise ValueError(f"constitution lease mutation forbidden: lease_id={lid}")
                else:
                    lease_rec = dict(lease)
                    lease_rec["stored_at"] = now
                    conn.execute(
                        """INSERT INTO constitution_leases(lease_id, run_id, provider_id, packet_id,
                           constitution_version, constitution_hash, lease_fingerprint, payload_json, stored_at)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (
                            lid,
                            lease.get("run_id") or rid,
                            lease.get("provider_id"),
                            lease.get("packet_id"),
                            lease.get("constitution_version"),
                            lease.get("constitution_hash"),
                            lease.get("lease_fingerprint"),
                            dumps(lease_rec),
                            now,
                        ),
                    )

            conn.execute(
                """INSERT INTO runs(
                id, project_id, task_id, packet_id, provider_id, repository, repository_local_path,
                baseline_ref, baseline_commit, requested_target_branch, execution_branch, operating_mode,
                risk, status, execution_mode, scope_fingerprint, constitution_version, constitution_hash,
                constitution_lease_id, constitution_lease_fingerprint, task_lock_id, payload_json,
                created_at, updated_at, started_at, finished_at, idempotency_key, row_version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
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
                    int(record.get("row_version") or 1),
                ),
            )
            event = {
                "id": new_id("re"),
                "run_id": rid,
                "event_type": event_type,
                "summary": event_summary,
                "actor": event_actor,
                "metadata": event_metadata or {},
                "created_at": now,
            }
            conn.execute(
                """INSERT INTO run_events(id, run_id, event_type, summary, actor, metadata_json, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    event["id"],
                    rid,
                    event_type,
                    event_summary,
                    event_actor,
                    dumps(event_metadata or {}),
                    now,
                ),
            )
        return record

    def transition_run_with_event(
        self,
        run: dict[str, Any],
        *,
        expected_row_version: int | None = None,
        event_type: str,
        event_summary: str = "",
        event_actor: str = "system",
        event_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically persist run state change + audit event with optimistic concurrency."""
        record = dict(run)
        rid = str(record.get("id") or "")
        if not rid:
            raise ValueError("run id required")
        now = utc_now_iso()
        record["updated_at"] = now
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT id, row_version, payload_json FROM runs WHERE id=?", (rid,)
            ).fetchone()
            if not existing:
                raise KeyError(f"Run not found: {rid}")
            current_ver = int(existing[1] or 1)
            if expected_row_version is not None and current_ver != int(expected_row_version):
                raise ValueError(
                    f"stale run transition rejected: expected row_version={expected_row_version} "
                    f"have={current_ver} run_id={rid}"
                )
            # If caller didn't pass expected version, still use DB version as base
            if expected_row_version is None:
                # Allow only if payload row_version matches DB (best-effort)
                payload_ver = int(record.get("row_version") or current_ver)
                if payload_ver != current_ver:
                    raise ValueError(
                        f"stale run transition rejected: payload row_version={payload_ver} "
                        f"have={current_ver} run_id={rid}"
                    )
            new_ver = current_ver + 1
            record["row_version"] = new_ver
            cur = conn.execute(
                """UPDATE runs SET project_id=?, task_id=?, packet_id=?, provider_id=?, repository=?,
                repository_local_path=?, baseline_ref=?, baseline_commit=?, requested_target_branch=?,
                execution_branch=?, operating_mode=?, risk=?, status=?, execution_mode=?,
                scope_fingerprint=?, constitution_version=?, constitution_hash=?, constitution_lease_id=?,
                constitution_lease_fingerprint=?, task_lock_id=?, payload_json=?, updated_at=?,
                started_at=?, finished_at=?, idempotency_key=?, row_version=?
                WHERE id=? AND row_version=?""",
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
                    new_ver,
                    rid,
                    current_ver,
                ),
            )
            if cur.rowcount == 0:
                raise ValueError(f"stale run transition race: run_id={rid}")
            conn.execute(
                """INSERT INTO run_events(id, run_id, event_type, summary, actor, metadata_json, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    new_id("re"),
                    rid,
                    event_type,
                    event_summary,
                    event_actor,
                    dumps(event_metadata or {}),
                    now,
                ),
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
        """Legacy/migration helper: append history + upsert effective current.

        Live approval path must use commit_run_approval() for atomic authority.
        """
        record = dict(payload)
        record.setdefault("id", new_id("rap"))
        record.setdefault("approval_id", record["id"])
        now = utc_now_iso()
        record.setdefault("created_at", now)
        record["updated_at"] = now
        record["immutable"] = True
        with self.db.transaction() as conn:
            self._insert_approval_history_conn(conn, record)
            self._upsert_effective_approval_conn(conn, record)
        return record

    def list_run_approvals(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Effective current approvals (latest valid projection, not full history)."""
        with self.db.transaction() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT payload_json FROM run_approvals WHERE run_id=?", (run_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT payload_json FROM run_approvals").fetchall()
        return [loads(r[0], {}) for r in rows]

    def list_run_approval_history(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT payload_json FROM run_approval_history WHERE run_id=? ORDER BY created_at ASC",
                    (run_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM run_approval_history ORDER BY created_at ASC"
                ).fetchall()
        return [loads(r[0], {}) for r in rows]

    def _insert_approval_history_conn(self, conn: Any, record: dict[str, Any]) -> None:
        aid = str(record.get("approval_id") or record.get("id") or new_id("rap"))
        record["approval_id"] = aid
        record["id"] = aid
        conn.execute(
            """INSERT INTO run_approval_history(
               approval_id, run_id, requirement_type, decision, scope_fingerprint,
               constitution_hash, constitution_lease_id, constitution_lease_fingerprint,
               actor, note, created_at, idempotency_key, payload_json, immutable)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (
                aid,
                str(record.get("run_id") or ""),
                str(record.get("requirement_type") or ""),
                str(record.get("decision") or ""),
                record.get("scope_fingerprint") or record.get("scope"),
                record.get("constitution_hash"),
                record.get("constitution_lease_id"),
                record.get("constitution_lease_fingerprint"),
                record.get("actor"),
                record.get("note"),
                record.get("created_at") or utc_now_iso(),
                record.get("idempotency_key") or None,
                dumps(record),
            ),
        )

    def _upsert_effective_approval_conn(self, conn: Any, record: dict[str, Any]) -> None:
        aid = str(record.get("approval_id") or record.get("id") or new_id("rap"))
        now = record.get("updated_at") or utc_now_iso()
        conn.execute(
            """INSERT INTO run_approvals(id, run_id, requirement_type, decision, scope_fingerprint,
               constitution_hash, constitution_lease_id, note, actor, payload_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id, requirement_type) DO UPDATE SET
               id=excluded.id, decision=excluded.decision, scope_fingerprint=excluded.scope_fingerprint,
               constitution_hash=excluded.constitution_hash, constitution_lease_id=excluded.constitution_lease_id,
               note=excluded.note, actor=excluded.actor, payload_json=excluded.payload_json,
               updated_at=excluded.updated_at""",
            (
                aid,
                str(record.get("run_id") or ""),
                str(record.get("requirement_type") or ""),
                str(record.get("decision") or ""),
                record.get("scope_fingerprint") or record.get("scope"),
                record.get("constitution_hash"),
                record.get("constitution_lease_id"),
                record.get("note"),
                record.get("actor"),
                dumps(record),
                record.get("created_at") or now,
                now,
            ),
        )

    def commit_run_approval(
        self,
        *,
        run_id: str,
        expected_row_version: int,
        approval_payload: dict[str, Any],
        event_type: str = "approval_recorded",
        event_summary: str = "",
        event_actor: str = "shan",
        event_metadata: dict[str, Any] | None = None,
        transition_reason: str = "",
    ) -> dict[str, Any]:
        """Atomically: history + effective approval + optional run transition + event.

        Evaluates approval set only after history insert inside the same transaction.
        """
        from buildforme.run_state import can_transition, is_terminal, transition_run
        from governance.constitution_binding_guard import validate_approval_binding

        rid = str(run_id)
        approval = dict(approval_payload)
        approval.setdefault("id", new_id("rap"))
        approval["approval_id"] = str(approval.get("approval_id") or approval["id"])
        approval["id"] = approval["approval_id"]
        now = utc_now_iso()
        approval.setdefault("created_at", now)
        approval["updated_at"] = now
        approval["immutable"] = True
        idemp = str(approval.get("idempotency_key") or "").strip() or None
        if idemp:
            approval["idempotency_key"] = idemp

        with self.db.transaction() as conn:
            # —— Idempotent replay (no new history/event) ——
            if idemp:
                hit = conn.execute(
                    "SELECT payload_json FROM run_approval_history WHERE idempotency_key=?",
                    (idemp,),
                ).fetchone()
                if hit:
                    prev = loads(hit[0], {})
                    if str(prev.get("run_id")) != rid:
                        raise ValueError("idempotency key bound to a different run")
                    if str(prev.get("requirement_type")) != str(approval.get("requirement_type")):
                        raise ValueError("idempotency key conflicts: requirement_type mismatch")
                    if str(prev.get("decision")) != str(approval.get("decision")):
                        raise ValueError("idempotency key conflicts: decision mismatch")
                    if str(prev.get("scope_fingerprint") or prev.get("scope") or "") != str(
                        approval.get("scope_fingerprint") or approval.get("scope") or ""
                    ):
                        raise ValueError("idempotency key conflicts: scope_fingerprint mismatch")
                    run_row = conn.execute(
                        "SELECT payload_json FROM runs WHERE id=?", (rid,)
                    ).fetchone()
                    if not run_row:
                        raise KeyError(f"Run not found: {rid}")
                    return {
                        "approval": prev,
                        "run": loads(run_row[0], {}),
                        "event": None,
                        "replayed": True,
                    }

            existing = conn.execute(
                "SELECT id, row_version, payload_json FROM runs WHERE id=?", (rid,)
            ).fetchone()
            if not existing:
                raise KeyError(f"Run not found: {rid}")
            current_ver = int(existing[1] or 1)
            if current_ver != int(expected_row_version):
                raise ValueError(
                    f"stale approval rejected: expected row_version={expected_row_version} "
                    f"have={current_ver} run_id={rid}"
                )
            run = loads(existing[2], {})
            run["row_version"] = current_ver
            status = str(run.get("status") or "")

            if is_terminal(status) and status not in {"approved"}:
                raise ValueError(f"cannot approve terminal run status={status}")
            if status == "rejected":
                raise ValueError("cannot approve rejected run")
            if status not in {
                "awaiting_approval",
                "awaiting_preflight",
                "approved",
                "draft",
            }:
                raise ValueError(f"cannot record approval from status {status}")

            lease_id = str(run.get("constitution_lease_id") or "")
            if lease_id:
                lease_row = conn.execute(
                    "SELECT payload_json FROM constitution_leases WHERE lease_id=?",
                    (lease_id,),
                ).fetchone()
                if not lease_row:
                    raise ValueError("constitution lease missing for run")

            for field in (
                "constitution_version",
                "constitution_hash",
                "constitution_lease_id",
            ):
                if str(approval.get(field) or "") != str(run.get(field) or ""):
                    raise ValueError(f"approval {field} does not match current run")

            # Scope must match recalculated run material (caller may not invent scope)
            from buildforme.governance import compute_run_scope_fingerprint

            packet = run.get("packet") if isinstance(run.get("packet"), dict) else None
            live_scope = compute_run_scope_fingerprint(run, packet)
            provided_scope = str(
                approval.get("scope_fingerprint") or approval.get("scope") or ""
            )
            if provided_scope != live_scope:
                raise ValueError(
                    "approval scope fingerprint does not match current run scope"
                )
            approval["scope_fingerprint"] = live_scope
            approval["scope"] = live_scope

            problems = validate_approval_binding(
                approval,
                run,
                expected_scope_fingerprint=live_scope,
            )
            if problems:
                raise ValueError("approval binding invalid: " + "; ".join(problems))

            # Rejection finality: prior effective rejection for this run blocks approve
            if str(approval.get("decision")) == "approved":
                eff_rows = conn.execute(
                    "SELECT decision FROM run_approvals WHERE run_id=?", (rid,)
                ).fetchall()
                if any(str(r[0]) == "rejected" for r in eff_rows):
                    raise ValueError(
                        "run has a rejected approval requirement; cannot approve further"
                    )

            try:
                self._insert_approval_history_conn(conn, approval)
            except Exception as exc:
                raise ValueError(f"approval history insert failed: {exc}") from exc

            self._upsert_effective_approval_conn(conn, approval)

            # Evaluate effective approval set inside transaction
            scope_fp = live_scope
            eff = conn.execute(
                "SELECT payload_json FROM run_approvals WHERE run_id=?", (rid,)
            ).fetchall()
            approved_types: set[str] = set()
            rejected_any = False
            for erow in eff:
                item = loads(erow[0], {})
                probs = validate_approval_binding(
                    item, run, expected_scope_fingerprint=scope_fp
                )
                if probs:
                    continue
                if str(item.get("decision")) == "approved":
                    approved_types.add(str(item.get("requirement_type")))
                if str(item.get("decision")) == "rejected":
                    rejected_any = True

            required = [str(x) for x in (run.get("approval_requirements") or [])]
            derived_status = status
            transition_event_type = None
            transition_summary = None

            if str(approval.get("decision")) == "rejected" or rejected_any:
                if can_transition(status, "rejected"):
                    derived_status = "rejected"
                    transition_event_type = "approval_rejected"
                    transition_summary = str(
                        approval.get("note") or "approval rejected"
                    )
            elif (
                status == "awaiting_approval"
                and required
                and all(req in approved_types for req in required)
            ):
                if can_transition(status, "approved"):
                    derived_status = "approved"
                    transition_event_type = "run_approved"
                    transition_summary = (
                        "all required approvals present for current scope and constitution lease"
                    )

            record = dict(run)
            if derived_status != status:
                record = transition_run(
                    record,
                    derived_status,
                    event_actor,
                    transition_reason or transition_summary or "",
                )
            record["updated_at"] = now

            new_ver = current_ver + 1
            record["row_version"] = new_ver
            cur = conn.execute(
                """UPDATE runs SET project_id=?, task_id=?, packet_id=?, provider_id=?, repository=?,
                repository_local_path=?, baseline_ref=?, baseline_commit=?, requested_target_branch=?,
                execution_branch=?, operating_mode=?, risk=?, status=?, execution_mode=?,
                scope_fingerprint=?, constitution_version=?, constitution_hash=?, constitution_lease_id=?,
                constitution_lease_fingerprint=?, task_lock_id=?, payload_json=?, updated_at=?,
                started_at=?, finished_at=?, idempotency_key=?, row_version=?
                WHERE id=? AND row_version=?""",
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
                    new_ver,
                    rid,
                    current_ver,
                ),
            )
            if cur.rowcount == 0:
                raise ValueError(f"stale approval race: run_id={rid}")

            meta = dict(event_metadata or {})
            meta.setdefault("requirement_type", approval.get("requirement_type"))
            meta.setdefault("decision", approval.get("decision"))
            meta.setdefault("scope_fingerprint", scope_fp)
            meta.setdefault("approval_id", approval.get("approval_id"))
            meta.setdefault("resulting_status", record.get("status"))
            meta.setdefault("constitution_lease_id", run.get("constitution_lease_id"))

            conn.execute(
                """INSERT INTO run_events(id, run_id, event_type, summary, actor, metadata_json, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    new_id("re"),
                    rid,
                    event_type,
                    event_summary,
                    event_actor,
                    dumps(meta),
                    now,
                ),
            )
            if transition_event_type and derived_status != status:
                conn.execute(
                    """INSERT INTO run_events(id, run_id, event_type, summary, actor, metadata_json, created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        new_id("re"),
                        rid,
                        transition_event_type,
                        transition_summary or "",
                        event_actor,
                        dumps(meta),
                        now,
                    ),
                )

            event = {
                "event_type": event_type,
                "summary": event_summary,
                "actor": event_actor,
                "metadata": meta,
            }
            return {
                "approval": approval,
                "run": record,
                "event": event,
                "replayed": False,
            }

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
        """Append-only evidence persistence with independent fingerprint validation.

        Never silently repairs a mismatched fingerprint. Never overwrites an ID.
        """
        from buildforme.evidence import validate_evidence_for_storage

        record = dict(evidence)
        eid = str(record.get("evidence_id") or record.get("id") or new_id("ev"))
        record["evidence_id"] = eid
        record["id"] = eid

        # Validate before any storage metadata mutation that is not in fingerprint material.
        # saved_at/sequence are excluded from fingerprint by design.
        problems = validate_evidence_for_storage(record)
        if problems:
            raise ValueError("evidence rejected: " + "; ".join(problems))

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
            # Re-check fingerprint after sequence/parent assignment is unnecessary:
            # those fields are not material. Persist the validated fingerprint as-is.
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

    def get_evidence_by_id(self, evidence_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM evidence WHERE evidence_id=?",
                (str(evidence_id),),
            ).fetchone()
        if not row:
            raise KeyError(f"Evidence not found: {evidence_id}")
        return loads(row[0], {})

    def get_latest_execution_evidence(self, run_id: str) -> dict[str, Any]:
        """Latest execution-kind evidence for a run (skips founder_decision records)."""
        from buildforme.evidence import EVIDENCE_KIND_EXECUTION

        for item in self.list_run_evidence(run_id=run_id, limit=50):
            kind = str(item.get("evidence_kind") or "")
            if kind == EVIDENCE_KIND_EXECUTION or (
                not kind and isinstance(item.get("process"), dict)
            ):
                return item
        raise KeyError(f"Execution evidence not found for run: {run_id}")

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

    def commit_founder_decision(
        self,
        *,
        run: dict[str, Any],
        expected_row_version: int,
        decision_evidence: dict[str, Any],
        event_type: str = "founder_review_decision",
        event_summary: str = "",
        event_actor: str = "shan",
        event_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically: validate parent evidence + insert decision evidence + update run + event.

        On any failure the entire transaction rolls back — run cannot complete without evidence.
        """
        from buildforme.evidence import (
            EVIDENCE_KIND_EXECUTION,
            EVIDENCE_KIND_FOUNDER_DECISION,
            validate_founder_decision_for_storage,
        )

        record = dict(run)
        rid = str(record.get("id") or "")
        if not rid:
            raise ValueError("run id required")
        decision_ev = dict(decision_evidence)
        eid = str(decision_ev.get("evidence_id") or decision_ev.get("id") or new_id("ev-fd"))
        decision_ev["evidence_id"] = eid
        decision_ev["id"] = eid
        decision_ev["evidence_kind"] = EVIDENCE_KIND_FOUNDER_DECISION
        decision_ev["run_id"] = rid
        decision_ev["immutable"] = True

        problems = validate_founder_decision_for_storage(decision_ev)
        if problems:
            raise ValueError("founder decision evidence rejected: " + "; ".join(problems))

        parent_id = str(
            decision_ev.get("parent_evidence_id") or decision_ev.get("execution_evidence_id") or ""
        )
        if not parent_id:
            raise ValueError("parent execution evidence required")

        resulting = str(decision_ev.get("resulting_status") or "")
        if str(record.get("status") or "") != resulting:
            raise ValueError(
                f"inconsistent resulting status: run has {record.get('status')!r} "
                f"decision evidence has {resulting!r}"
            )

        now = utc_now_iso()
        decision_ev.setdefault("saved_at", now)
        record["updated_at"] = now
        meta = dict(event_metadata or {})
        meta.setdefault("decision", decision_ev.get("decision"))
        meta.setdefault("actor", event_actor)
        meta.setdefault("evidence_id", eid)
        meta.setdefault("parent_evidence_id", parent_id)
        meta.setdefault("resulting_status", resulting)

        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT id, row_version, payload_json FROM runs WHERE id=?", (rid,)
            ).fetchone()
            if not existing:
                raise KeyError(f"Run not found: {rid}")
            current_ver = int(existing[1] or 1)
            if current_ver != int(expected_row_version):
                raise ValueError(
                    f"stale founder decision rejected: expected row_version={expected_row_version} "
                    f"have={current_ver} run_id={rid}"
                )

            # Parent execution evidence must exist, belong to this run, and match fingerprint
            parent_row = conn.execute(
                "SELECT payload_json, evidence_fingerprint FROM evidence WHERE evidence_id=?",
                (parent_id,),
            ).fetchone()
            if not parent_row:
                raise ValueError(f"parent execution evidence not found: {parent_id}")
            parent_payload = loads(parent_row[0], {})
            if str(parent_payload.get("run_id") or "") != rid:
                raise ValueError(
                    f"parent evidence {parent_id} belongs to run "
                    f"{parent_payload.get('run_id')!r}, not {rid!r}"
                )
            parent_kind = str(parent_payload.get("evidence_kind") or "")
            parent_is_execution = parent_kind == EVIDENCE_KIND_EXECUTION or (
                not parent_kind and isinstance(parent_payload.get("process"), dict)
            )
            if not parent_is_execution:
                raise ValueError(
                    f"parent evidence must be execution evidence, got kind={parent_kind!r}"
                )
            expected_parent_fp = str(decision_ev.get("parent_evidence_fingerprint") or "")
            actual_parent_fp = str(
                parent_payload.get("evidence_fingerprint") or parent_row[1] or ""
            )
            if not expected_parent_fp or expected_parent_fp != actual_parent_fp:
                raise ValueError(
                    "parent evidence fingerprint mismatch or missing — refusing decision"
                )

            # Reject duplicate decision evidence ID
            if conn.execute(
                "SELECT evidence_id FROM evidence WHERE evidence_id=?", (eid,)
            ).fetchone():
                raise ValueError(f"evidence mutation forbidden: {eid} is append-only")

            # Reject terminal replay: a prior terminal founder decision is final
            prior_decisions = conn.execute(
                "SELECT payload_json FROM evidence WHERE run_id=?", (rid,)
            ).fetchall()
            terminal_results = {"completed", "rejected", "blocked"}
            for prow in prior_decisions:
                prev = loads(prow[0], {})
                if str(prev.get("evidence_kind") or "") != EVIDENCE_KIND_FOUNDER_DECISION:
                    continue
                prev_result = str(prev.get("resulting_status") or "")
                if prev_result in terminal_results:
                    raise ValueError(
                        f"founder decision already terminal for run {rid} "
                        f"(evidence_id={prev.get('evidence_id')}, "
                        f"resulting_status={prev_result}); replay rejected"
                    )
                # Non-terminal prior decisions (e.g. request_changes) allowed; continue

            prior_count = conn.execute(
                "SELECT COUNT(*) FROM evidence WHERE run_id=?", (rid,)
            ).fetchone()[0]
            decision_ev["sequence"] = int(prior_count) + 1
            decision_ev.setdefault("attempt", decision_ev["sequence"])
            decision_ev["parent_evidence_id"] = parent_id

            conn.execute(
                """INSERT INTO evidence(evidence_id, run_id, sequence, attempt, parent_evidence_id,
                   payload_json, evidence_fingerprint, saved_at, immutable)
                   VALUES (?,?,?,?,?,?,?,?,1)""",
                (
                    eid,
                    rid,
                    decision_ev["sequence"],
                    decision_ev.get("attempt"),
                    parent_id,
                    dumps(decision_ev),
                    decision_ev.get("evidence_fingerprint"),
                    decision_ev["saved_at"],
                ),
            )

            new_ver = current_ver + 1
            record["row_version"] = new_ver
            # Attach decision evidence pointer on run payload
            ids = list(record.get("evidence_ids") or [])
            if eid not in ids:
                ids.append(eid)
            record["evidence_ids"] = ids
            record["founder_decision_evidence_id"] = eid
            record["founder_decision_fingerprint"] = decision_ev.get("evidence_fingerprint")

            cur = conn.execute(
                """UPDATE runs SET project_id=?, task_id=?, packet_id=?, provider_id=?, repository=?,
                repository_local_path=?, baseline_ref=?, baseline_commit=?, requested_target_branch=?,
                execution_branch=?, operating_mode=?, risk=?, status=?, execution_mode=?,
                scope_fingerprint=?, constitution_version=?, constitution_hash=?, constitution_lease_id=?,
                constitution_lease_fingerprint=?, task_lock_id=?, payload_json=?, updated_at=?,
                started_at=?, finished_at=?, idempotency_key=?, row_version=?
                WHERE id=? AND row_version=?""",
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
                    new_ver,
                    rid,
                    current_ver,
                ),
            )
            if cur.rowcount == 0:
                raise ValueError(f"stale founder decision race: run_id={rid}")

            conn.execute(
                """INSERT INTO run_events(id, run_id, event_type, summary, actor, metadata_json, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    new_id("re"),
                    rid,
                    event_type,
                    event_summary,
                    event_actor,
                    dumps(meta),
                    now,
                ),
            )

        return {
            "run": record,
            "decision_evidence": decision_ev,
            "event": {
                "event_type": event_type,
                "summary": event_summary,
                "actor": event_actor,
                "metadata": meta,
            },
        }

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

    # —— Project execution controls (SQLite authority) ——
    def get_project_execution_control(self, project_id: str) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM project_execution_controls WHERE project_id=?",
                (str(project_id),),
            ).fetchone()
        return loads(row[0], {}) if row else None

    def set_project_execution_control(
        self,
        project_id: str,
        *,
        execution_status: str,
        reason: str = "",
        actor: str = "shan",
    ) -> dict[str, Any]:
        now = utc_now_iso()
        record = {
            "project_id": project_id,
            "execution_status": execution_status,
            "reason": str(reason or ""),
            "actor": actor,
            "updated_at": now,
            "explicit": True,
        }
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO project_execution_controls(project_id, execution_status, reason, actor, updated_at, payload_json)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(project_id) DO UPDATE SET
                   execution_status=excluded.execution_status, reason=excluded.reason,
                   actor=excluded.actor, updated_at=excluded.updated_at, payload_json=excluded.payload_json""",
                (
                    project_id,
                    execution_status,
                    record["reason"],
                    actor,
                    now,
                    dumps(record),
                ),
            )
        return record

    def list_project_execution_controls(self) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM project_execution_controls ORDER BY project_id"
            ).fetchall()
        return [loads(r[0], {}) for r in rows]

    def set_migration_cutover(self, marker: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO schema_meta(key, value) VALUES ('migration_cutover', ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (str(marker),),
            )

    def get_migration_cutover(self) -> str | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='migration_cutover'"
            ).fetchone()
        if not row or not row[0]:
            return None
        return str(row[0])

    def migrate_from_json(
        self,
        runtime_dir: Path,
        *,
        dry_run: bool = False,
        cutover: bool = True,
    ) -> dict[str, Any]:
        """Idempotent import from legacy JSON runtime files with backup and cutover marker.

        Authority after cutover: SQLite only for Stage 6 execution facts.
        dry_run previews counts without writing DB (still creates backup listing).
        """
        import json
        import shutil

        runtime_dir = Path(runtime_dir)
        stamp = utc_now_iso().replace(":", "")
        backup = runtime_dir / f"json_backup_{stamp}"
        report: dict[str, Any] = {
            "backup": str(backup),
            "imported": {},
            "preview": {},
            "errors": [],
            "malformed": [],
            "dry_run": bool(dry_run),
            "cutover": False,
            "integrity": None,
        }
        sources = (
            "runs.json",
            "run_events.json",
            "run_approvals.json",
            "constitution_leases.json",
            "task_locks.json",
            "repository_locks.json",
            "repository_bindings.json",
            "run_evidence.json",
            "execution_control.json",
            "project_execution_controls.json",
            "providers.json",
            "provider_acks.json",
        )
        if runtime_dir.exists():
            backup.mkdir(parents=True, exist_ok=True)
            for name in sources:
                src = runtime_dir / name
                if src.exists():
                    shutil.copy2(src, backup / name)

        def _load(name: str, list_key: str) -> list[dict[str, Any]]:
            path = runtime_dir / name
            if not path.exists():
                return []
            try:
                data = json.loads(path.read_text(encoding="utf-8") or "{}")
            except Exception as exc:
                report["errors"].append(f"{name}: {exc}")
                return []
            if not isinstance(data, dict):
                report["malformed"].append(f"{name}: root not object")
                return []
            items = data.get(list_key) or data.get("items") or []
            if name == "execution_control.json" and isinstance(data, dict) and "kill_switch_active" in data:
                return [data]
            out = []
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict):
                    out.append(item)
                else:
                    report["malformed"].append(f"{name}: non-object item")
            return out

        if dry_run:
            report["preview"] = {
                "runs": len(_load("runs.json", "runs")),
                "leases": len(_load("constitution_leases.json", "leases")),
                "bindings": len(_load("repository_bindings.json", "bindings")),
                "events": len(_load("run_events.json", "events")),
                "approvals": len(_load("run_approvals.json", "approvals")),
                "task_locks": len(_load("task_locks.json", "locks")),
                "repository_locks": len(_load("repository_locks.json", "locks")),
                "evidence": len(_load("run_evidence.json", "evidence")),
                "project_controls": len(_load("project_execution_controls.json", "controls")),
            }
            report["db"] = self.db.pragmas()
            return report

        # Runs
        count = 0
        for run in _load("runs.json", "runs"):
            if run.get("id"):
                try:
                    self.save_run(run)
                    count += 1
                except Exception as exc:
                    report["errors"].append(f"run {run.get('id')}: {exc}")
        report["imported"]["runs"] = count

        # Events (after runs so FK holds)
        count = 0
        for ev in _load("run_events.json", "events"):
            rid = str(ev.get("run_id") or "")
            if not rid:
                report["malformed"].append("run_event missing run_id")
                continue
            try:
                self.append_run_event(
                    rid,
                    str(ev.get("event_type") or ev.get("type") or "imported"),
                    str(ev.get("summary") or ""),
                    actor=str(ev.get("actor") or "system"),
                    metadata=ev.get("metadata") if isinstance(ev.get("metadata"), dict) else {},
                )
                count += 1
            except Exception as exc:
                report["errors"].append(f"event: {exc}")
        report["imported"]["events"] = count

        # Approvals
        count = 0
        for ap in _load("run_approvals.json", "approvals"):
            if ap.get("run_id") and ap.get("requirement_type"):
                try:
                    self.save_run_approval(ap)
                    count += 1
                except Exception as exc:
                    report["errors"].append(f"approval: {exc}")
        report["imported"]["approvals"] = count

        # Leases
        count = 0
        for lease in _load("constitution_leases.json", "leases"):
            if lease.get("lease_id"):
                try:
                    self.save_constitution_lease(lease)
                    count += 1
                except Exception as exc:
                    report["errors"].append(f"lease: {exc}")
        report["imported"]["leases"] = count

        # Task locks
        count = 0
        for lock in _load("task_locks.json", "locks"):
            if lock.get("task_key") and lock.get("active", True):
                try:
                    self.create_task_lock(lock)
                    count += 1
                except Exception as exc:
                    report["errors"].append(f"task_lock: {exc}")
        report["imported"]["task_locks"] = count

        # Repository locks
        count = 0
        for lock in _load("repository_locks.json", "locks"):
            if lock.get("repository") and lock.get("active", True):
                try:
                    self.create_repository_lock(lock)
                    count += 1
                except Exception as exc:
                    report["errors"].append(f"repo_lock: {exc}")
        report["imported"]["repository_locks"] = count

        # Bindings
        count = 0
        for b in _load("repository_bindings.json", "bindings"):
            try:
                self.register_repository_binding(b)
                count += 1
            except Exception as exc:
                report["errors"].append(f"binding: {exc}")
        report["imported"]["bindings"] = count

        # Evidence
        count = 0
        for ev in _load("run_evidence.json", "evidence"):
            if ev.get("run_id"):
                try:
                    # re-key so import can be replayed without mutation errors on second apply
                    if "evidence_id" in ev:
                        # only insert if missing
                        with self.db.transaction() as conn:
                            exists = conn.execute(
                                "SELECT evidence_id FROM evidence WHERE evidence_id=?",
                                (str(ev["evidence_id"]),),
                            ).fetchone()
                        if exists:
                            count += 1
                            continue
                    self.save_run_evidence(ev)
                    count += 1
                except Exception as exc:
                    report["errors"].append(f"evidence: {exc}")
        report["imported"]["evidence"] = count

        # Execution control
        for ctrl in _load("execution_control.json", "controls"):
            try:
                self.set_execution_control(
                    kill_switch_active=bool(ctrl.get("kill_switch_active")),
                    reason=str(ctrl.get("reason") or ""),
                    actor=str(ctrl.get("actor") or "system"),
                )
                report["imported"]["execution_control"] = 1
            except Exception as exc:
                report["errors"].append(f"execution_control: {exc}")

        # Project controls
        count = 0
        for pc in _load("project_execution_controls.json", "controls"):
            pid = str(pc.get("project_id") or "")
            if not pid:
                continue
            try:
                self.set_project_execution_control(
                    pid,
                    execution_status=str(pc.get("execution_status") or "locked"),
                    reason=str(pc.get("reason") or ""),
                    actor=str(pc.get("actor") or "system"),
                )
                count += 1
            except Exception as exc:
                report["errors"].append(f"project_control: {exc}")
        report["imported"]["project_controls"] = count

        # Provider acks from providers.json constitution fields
        count = 0
        for prov in _load("providers.json", "providers"):
            pid = str(prov.get("provider_id") or "")
            if pid and prov.get("constitution_acknowledged"):
                try:
                    self.set_provider_constitution_ack(pid, prov)
                    count += 1
                except Exception as exc:
                    report["errors"].append(f"provider_ack: {exc}")
        report["imported"]["provider_acks"] = count

        report["db"] = self.db.pragmas()
        report["integrity"] = report["db"].get("integrity_check")
        if cutover and not report["errors"]:
            self.set_migration_cutover(f"sqlite_authority_{stamp}")
            report["cutover"] = True
            report["cutover_marker"] = self.get_migration_cutover()
        elif cutover and report["errors"]:
            report["cutover"] = False
            report["errors"].append("cutover withheld because import reported errors")
        return report
