"""Transactional Stage 6 persistence API backed by SQLite."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from buildforme.db import ExecutionDB, dumps, loads, new_id, row_to_dict
from buildforme.storage import utc_now_iso

# Bound at admission / scope / lease / worktree — never rewritten by generic mutation.
PROTECTED_AUTHORITY_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "project_id",
        "task_id",
        "packet_id",
        "provider_id",
        "repository",
        "repository_local_path",
        "baseline_ref",
        "baseline_commit",
        "requested_target_branch",
        "target_branch",
        "execution_branch",
        "operating_mode",
        "risk",
        "execution_mode",
        "mode",
        "transport",
        "requested_capabilities",
        "scope_fingerprint",
        "constitution_version",
        "constitution_hash",
        "constitution_lease_id",
        "constitution_lease_fingerprint",
        "constitution_lease",
        "task_lock_id",
        "parent_run_id",
        "attempt",
        "max_attempts",
        "idempotency_key",
        "created_at",
    }
)

# Lifecycle fields are storage-owned. Runtime callers may propose a target status,
# but they cannot author history, timestamps, row versions, or immutable identity.
_STORAGE_OWNED_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "created_at",
        "updated_at",
        "row_version",
        "status",
        "status_history",
        "started_at",
        "finished_at",
    }
)

# Storage-owned metadata allowlists (mutation_type -> mutable payload fields).
# No protected authority field may appear in any allowlist.
MUTATION_METADATA_ALLOWLISTS: dict[str, frozenset[str]] = {
    "preflight_result": frozenset({"preflight", "approval_requirements"}),
    "worktree_prepared": frozenset(
        {"worktree", "worktree_path", "workspace_root", "provider_version"}
    ),
    "process_started": frozenset(
        {"worktree", "worktree_path", "workspace_root", "provider_version"}
    ),
    "process_result": frozenset({"process_result", "result_summary"}),
    "verification_result": frozenset({"verification"}),
    "execution_evidence_link": frozenset(
        {
            "evidence",
            "evidence_ids",
            "final_head_sha",
            "head_commit",
            "worktree",
            "worktree_path",
        }
    ),
    "review_package": frozenset(
        {
            "review",
            "result_summary",
            "constitution_compliance",
            "constitution_reminder",
        }
    ),
    "constitution_compliance": frozenset(
        {"constitution_compliance", "constitution_reminder"}
    ),
    "failure_detail": frozenset(
        {"process_result", "result_summary", "preflight", "dry_run_result"}
    ),
    "supervised_finished": frozenset(
        {
            "process_result",
            "evidence",
            "evidence_ids",
            "verification",
            "review",
            "result_summary",
            "constitution_compliance",
            "constitution_reminder",
            "final_head_sha",
            "head_commit",
            "worktree",
            "worktree_path",
            "workspace_root",
            "provider_version",
        }
    ),
    "dry_run_finished": frozenset(
        {
            "dry_run_result",
            "result_summary",
            "constitution_compliance",
            "constitution_reminder",
        }
    ),
    "status_transition": frozenset(),
    "cancel": frozenset({"process_result", "result_summary"}),
}

# Lifecycle constraints are storage authority. Caller-supplied require_db_status_in
# may narrow these sets, but can never broaden them.
_MUTATION_ALLOWED_STATUSES: dict[str, frozenset[str]] = {
    "preflight_result": frozenset(
        {"awaiting_preflight", "awaiting_approval", "approved", "queued"}
    ),
    "worktree_prepared": frozenset({"approved", "queued", "starting"}),
    "process_started": frozenset({"approved", "queued", "starting"}),
    "process_result": frozenset({"queued", "starting", "running", "cancel_requested"}),
    "verification_result": frozenset({"running", "needs_review"}),
    "execution_evidence_link": frozenset({"running", "needs_review"}),
    "review_package": frozenset({"running", "needs_review"}),
    "constitution_compliance": frozenset(
        {"queued", "starting", "running", "needs_review"}
    ),
    "failure_detail": frozenset(
        {"approved", "queued", "starting", "running", "cancel_requested"}
    ),
    "supervised_finished": frozenset({"queued", "starting", "running"}),
    "dry_run_finished": frozenset({"running", "needs_review"}),
    "status_transition": frozenset(
        {
            "draft",
            "awaiting_preflight",
            "awaiting_approval",
            "approved",
            "queued",
            "starting",
            "running",
            "cancel_requested",
            "needs_review",
        }
    ),
    "cancel": frozenset(
        {
            "draft",
            "awaiting_preflight",
            "awaiting_approval",
            "approved",
            "queued",
            "starting",
            "running",
            "cancel_requested",
            "needs_review",
        }
    ),
}

# A mutation type authorizes only its own lifecycle edges. Passing a valid
# mutation label must never become a generic state-transition capability.
_MUTATION_ALLOWED_EDGES: dict[str, frozenset[tuple[str, str]]] = {
    "preflight_result": frozenset(
        {
            ("awaiting_preflight", "preflight_failed"),
            ("awaiting_preflight", "awaiting_approval"),
            ("awaiting_preflight", "approved"),
            ("awaiting_preflight", "blocked"),
            ("awaiting_approval", "approved"),
            ("awaiting_approval", "blocked"),
            ("approved", "blocked"),
            ("queued", "blocked"),
        }
    ),
    "worktree_prepared": frozenset(),
    "process_started": frozenset(
        {
            ("approved", "queued"),
            ("queued", "starting"),
            ("starting", "running"),
        }
    ),
    "process_result": frozenset(),
    "verification_result": frozenset(),
    "execution_evidence_link": frozenset(),
    "review_package": frozenset(),
    "constitution_compliance": frozenset(),
    "failure_detail": frozenset(
        {
            ("approved", "blocked"),
            ("queued", "failed"),
            ("starting", "failed"),
            ("running", "failed"),
            ("running", "timed_out"),
            ("cancel_requested", "failed"),
        }
    ),
    "supervised_finished": frozenset({("running", "needs_review")}),
    "dry_run_finished": frozenset(
        {
            ("running", "needs_review"),
            ("needs_review", "completed"),
        }
    ),
    "status_transition": frozenset({("draft", "awaiting_preflight")}),
    "cancel": frozenset(
        {
            ("draft", "rejected"),
            ("draft", "blocked"),
            ("awaiting_preflight", "blocked"),
            ("awaiting_approval", "rejected"),
            ("awaiting_approval", "blocked"),
            ("approved", "rejected"),
            ("approved", "blocked"),
            ("queued", "cancel_requested"),
            ("starting", "cancel_requested"),
            ("running", "cancel_requested"),
            ("cancel_requested", "cancelled"),
            ("needs_review", "rejected"),
            ("needs_review", "blocked"),
        }
    ),
}

# Metadata-only mutation classes that may commit without a state edge.
_MUTATION_ALLOW_SAME_STATE: frozenset[str] = frozenset(
    {
        "preflight_result",
        "worktree_prepared",
        "process_result",
        "verification_result",
        "execution_evidence_link",
        "review_package",
        "constitution_compliance",
        "failure_detail",
    }
)

# Mutation classes are also bound to the admitted execution mode. A live run
# must never complete through the dry-run authority path, or vice versa.
_MUTATION_ALLOWED_EXECUTION_MODES: dict[str, frozenset[str]] = {
    "preflight_result": frozenset({"dry_run", "live_supervised"}),
    "worktree_prepared": frozenset({"live_supervised"}),
    "process_started": frozenset({"dry_run", "live_supervised"}),
    "process_result": frozenset({"dry_run", "live_supervised"}),
    "verification_result": frozenset({"live_supervised"}),
    "execution_evidence_link": frozenset({"live_supervised"}),
    "review_package": frozenset({"live_supervised"}),
    "constitution_compliance": frozenset({"dry_run", "live_supervised"}),
    "failure_detail": frozenset({"dry_run", "live_supervised"}),
    "supervised_finished": frozenset({"live_supervised"}),
    "dry_run_finished": frozenset({"dry_run"}),
    "status_transition": frozenset({"dry_run", "live_supervised"}),
    "cancel": frozenset({"dry_run", "live_supervised"}),
}


def _values_equal(left: Any, right: Any) -> bool:
    if left is right:
        return True
    try:
        return dumps(left) == dumps(right)
    except Exception:
        return left == right


class Stage6Store:
    def __init__(self, db_path: Path | str):
        self.db = ExecutionDB(db_path)

    # —— Runs ——
    def save_run(
        self,
        run: dict[str, Any],
        *,
        expected_row_version: int | None = None,
        allow_unversioned: bool = False,
    ) -> dict[str, Any]:
        """Create a run or versioned-update an existing one.

        Runtime Stage 6 code must pass expected_row_version for updates, or use
        commit_run_mutation / transition_run_with_event. allow_unversioned is only
        for migration/import and explicit test fixture setup.
        """
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
                if expected_row_version is None and not allow_unversioned:
                    raise ValueError(
                        f"existing run update requires expected_row_version "
                        f"(run_id={rid}); use commit_run_mutation or pass version"
                    )
                if expected_row_version is not None and current_ver != int(expected_row_version):
                    raise ValueError(
                        f"stale run write rejected: expected row_version={expected_row_version} "
                        f"have={current_ver} run_id={rid}"
                    )
                # Unversioned setup path still bumps version for consistency
                base_ver = current_ver if expected_row_version is None else int(expected_row_version)
                if expected_row_version is None:
                    base_ver = current_ver
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

    def save_run_for_setup(self, run: dict[str, Any]) -> dict[str, Any]:
        """Explicit fixture/migration helper — not for runtime authority paths."""
        return self.save_run(run, allow_unversioned=True)

    def commit_run_mutation(
        self,
        run: dict[str, Any],
        *,
        expected_row_version: int,
        mutation_type: str,
        event_type: str,
        event_summary: str = "",
        event_actor: str = "system",
        event_metadata: dict[str, Any] | None = None,
        require_db_status_in: set[str] | frozenset[str] | None = None,
        transition_path: list[str] | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically apply a storage-authorized mutation and audit event(s).

        Runtime callers may submit a full run snapshot for compatibility, but
        storage never persists that snapshot wholesale. It loads the canonical
        database payload, validates proposed differences against a storage-owned
        policy, applies only permitted metadata changes, and derives lifecycle
        history/timestamps from validated state edges.
        """
        from buildforme.run_state import can_transition, is_terminal

        if mutation_type not in MUTATION_METADATA_ALLOWLISTS:
            raise ValueError(f"unknown mutation_type: {mutation_type!r}")

        proposed = dict(run)
        rid = str(proposed.get("id") or "")
        if not rid:
            raise ValueError("run id required")
        now = utc_now_iso()
        allow = MUTATION_METADATA_ALLOWLISTS[mutation_type]
        allowed_statuses = _MUTATION_ALLOWED_STATUSES[mutation_type]
        allowed_execution_modes = _MUTATION_ALLOWED_EXECUTION_MODES[mutation_type]

        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT id, row_version, payload_json, status FROM runs WHERE id=?",
                (rid,),
            ).fetchone()
            if not existing:
                raise KeyError(f"Run not found: {rid}")

            current_ver = int(existing[1] or 1)
            if current_ver != int(expected_row_version):
                raise ValueError(
                    f"stale run mutation rejected: expected row_version={expected_row_version} "
                    f"have={current_ver} run_id={rid}"
                )

            db_payload = loads(existing[2], {})
            if not isinstance(db_payload, dict):
                raise ValueError(f"run payload is not an object: run_id={rid}")
            db_status = str(existing[3] or db_payload.get("status") or "")
            new_status = str(proposed.get("status") or db_status)
            execution_mode = str(
                db_payload.get("execution_mode") or db_payload.get("mode") or "dry_run"
            ).strip().lower().replace("-", "_")

            if execution_mode not in allowed_execution_modes:
                raise ValueError(
                    f"mutation_type {mutation_type!r} not permitted for "
                    f"execution_mode {execution_mode!r}"
                )
            if db_status not in allowed_statuses:
                raise ValueError(
                    f"mutation_type {mutation_type!r} not permitted from status {db_status!r}"
                )
            if require_db_status_in is not None and db_status not in require_db_status_in:
                raise ValueError(
                    f"run mutation refused: db status {db_status!r} not in "
                    f"{sorted(require_db_status_in)}"
                )

            # Protected authority is immutable for every runtime mutation type.
            for field in PROTECTED_AUTHORITY_FIELDS:
                if field not in proposed:
                    continue
                if not _values_equal(proposed.get(field), db_payload.get(field)):
                    raise ValueError(
                        f"authority field mutation forbidden: {field} "
                        f"(mutation_type={mutation_type})"
                    )

            # Identify permitted metadata differences. Storage-owned lifecycle
            # fields are ignored here and reconstructed below; caller values are
            # never persisted.
            changed: list[str] = []
            for key in sorted(proposed.keys()):
                if key in _STORAGE_OWNED_FIELDS or key in PROTECTED_AUTHORITY_FIELDS:
                    continue
                if _values_equal(proposed.get(key), db_payload.get(key)):
                    continue
                if key not in allow:
                    raise ValueError(
                        f"unauthorized field change: {key} "
                        f"(mutation_type={mutation_type})"
                    )
                changed.append(key)

            # Validate state authority before constructing the new payload.
            edges: list[tuple[str, str]] = []
            if db_status != new_status:
                if is_terminal(db_status):
                    raise ValueError(
                        f"cannot overwrite terminal status {db_status!r} with {new_status!r}"
                    )
                if transition_path is not None:
                    path = [str(status) for status in transition_path]
                    if len(path) < 2:
                        raise ValueError("transition_path requires at least two statuses")
                    if path[0] != db_status:
                        raise ValueError(
                            f"transition_path start {path[0]!r} != db status {db_status!r}"
                        )
                    if path[-1] != new_status:
                        raise ValueError(
                            f"transition_path end {path[-1]!r} != proposed status {new_status!r}"
                        )
                    for index in range(len(path) - 1):
                        previous, resulting = path[index], path[index + 1]
                        if not can_transition(previous, resulting):
                            raise ValueError(
                                f"invalid transition edge in path: {previous!r} → {resulting!r}"
                            )
                        edges.append((previous, resulting))
                else:
                    if not can_transition(db_status, new_status):
                        raise ValueError(
                            f"invalid immediate transition {db_status!r} → {new_status!r} "
                            "(multi-hop requires explicit transition_path)"
                        )
                    edges.append((db_status, new_status))
            elif transition_path is not None:
                raise ValueError("transition_path is forbidden for a same-state mutation")

            if edges:
                allowed_edges = _MUTATION_ALLOWED_EDGES[mutation_type]
                forbidden_edges = [edge for edge in edges if edge not in allowed_edges]
                if forbidden_edges:
                    rendered = ", ".join(
                        f"{previous} → {resulting}"
                        for previous, resulting in forbidden_edges
                    )
                    raise ValueError(
                        f"mutation_type {mutation_type!r} does not authorize "
                        f"transition edge(s): {rendered}"
                    )
            elif mutation_type not in _MUTATION_ALLOW_SAME_STATE:
                raise ValueError(
                    f"mutation_type {mutation_type!r} requires an authorized status transition"
                )

            if (
                mutation_type == "preflight_result"
                and "approval_requirements" in changed
                and not edges
            ):
                raise ValueError(
                    "approval_requirements may change only with an authorized "
                    "preflight state edge"
                )

            # Apply a change set to canonical storage state; never replace the
            # database payload with the caller snapshot.
            record = dict(db_payload)
            for key in changed:
                record[key] = proposed.get(key)

            history_raw = db_payload.get("status_history") or []
            if not isinstance(history_raw, list):
                raise ValueError(f"run status_history is not a list: run_id={rid}")
            history = list(history_raw)
            for index, (previous, resulting) in enumerate(edges):
                reason = (
                    event_summary
                    if index == len(edges) - 1
                    else f"{event_type}: {previous} → {resulting}"
                )
                history.append(
                    {
                        "from": previous,
                        "to": resulting,
                        "actor": str(event_actor or "system"),
                        "reason": reason,
                        "at": now,
                    }
                )

            record["id"] = rid
            record["status"] = new_status
            record["status_history"] = history
            record["created_at"] = db_payload.get("created_at")
            record["updated_at"] = now
            if not record.get("started_at") and any(
                resulting in {"starting", "running"} for _, resulting in edges
            ):
                record["started_at"] = now
            if edges and is_terminal(new_status):
                record["finished_at"] = now

            new_ver = current_ver + 1
            record["row_version"] = new_ver

            outcome_record: dict[str, Any] | None = None
            if evidence is not None:
                from buildforme.outcome_evidence import validate_run_outcome_evidence

                outcome_record = dict(evidence)
                problems = validate_run_outcome_evidence(outcome_record)
                if problems:
                    raise ValueError("run outcome evidence rejected: " + "; ".join(problems))
                if str(outcome_record.get("run_id") or "") != rid:
                    raise ValueError("run outcome evidence run_id mismatch")
                if str(outcome_record.get("previous_status") or "") != db_status:
                    raise ValueError("run outcome evidence previous_status mismatch")
                if str(outcome_record.get("resulting_status") or "") != new_status:
                    raise ValueError("run outcome evidence resulting_status mismatch")
                if int(outcome_record.get("previous_row_version") or -1) != current_ver:
                    raise ValueError("run outcome evidence previous_row_version mismatch")
                evidence_id = str(outcome_record.get("evidence_id") or outcome_record.get("id") or "")
                if not evidence_id:
                    raise ValueError("run outcome evidence_id required")
                if conn.execute(
                    "SELECT evidence_id FROM evidence WHERE evidence_id=?", (evidence_id,)
                ).fetchone():
                    raise ValueError(f"evidence mutation forbidden: {evidence_id} is append-only")
                prior = int(
                    conn.execute("SELECT COUNT(*) FROM evidence WHERE run_id=?", (rid,)).fetchone()[0]
                )
                outcome_record["sequence"] = prior + 1
                outcome_record.setdefault("attempt", outcome_record["sequence"])
                outcome_record.setdefault("saved_at", now)
                parent = conn.execute(
                    "SELECT evidence_id FROM evidence WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
                    (rid,),
                ).fetchone()
                if parent:
                    outcome_record.setdefault("parent_evidence_id", parent[0])
                ids = list(record.get("evidence_ids") or [])
                if evidence_id not in ids:
                    ids.append(evidence_id)
                record["evidence_ids"] = ids
                record["outcome_evidence_id"] = evidence_id
                record["outcome_evidence_fingerprint"] = outcome_record.get("evidence_fingerprint")

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
                    new_status,
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
                raise ValueError(f"stale run mutation race: run_id={rid}")

            if outcome_record is not None:
                conn.execute(
                    """INSERT INTO evidence(evidence_id, run_id, sequence, attempt, parent_evidence_id,
                       payload_json, evidence_fingerprint, saved_at, immutable)
                       VALUES (?,?,?,?,?,?,?,?,1)""",
                    (
                        outcome_record["evidence_id"],
                        rid,
                        outcome_record["sequence"],
                        outcome_record.get("attempt"),
                        outcome_record.get("parent_evidence_id"),
                        dumps(outcome_record),
                        outcome_record.get("evidence_fingerprint"),
                        outcome_record["saved_at"],
                    ),
                )

            base_meta = dict(event_metadata or {})
            if outcome_record is not None:
                base_meta["evidence_id"] = outcome_record.get("evidence_id")
                base_meta["evidence_fingerprint"] = outcome_record.get("evidence_fingerprint")
            base_meta["mutation_type"] = mutation_type
            base_meta["fields_changed"] = changed
            base_meta["previous_row_version"] = current_ver
            base_meta["resulting_row_version"] = new_ver
            base_meta["actor"] = str(event_actor or "system")
            base_meta["timestamp"] = now

            if edges:
                path_value = [edges[0][0]] + [edge[1] for edge in edges]
                for index, (previous, resulting) in enumerate(edges):
                    reason = (
                        event_summary
                        if index == len(edges) - 1
                        else f"{event_type}: {previous} → {resulting}"
                    )
                    edge_meta = dict(base_meta)
                    edge_meta["previous_status"] = previous
                    edge_meta["resulting_status"] = resulting
                    edge_meta["path_index"] = index
                    edge_meta["path"] = path_value
                    edge_meta["reason"] = reason
                    stored_event_type = (
                        event_type if index == len(edges) - 1 else "status_transition"
                    )
                    conn.execute(
                        """INSERT INTO run_events(id, run_id, event_type, summary, actor, metadata_json, created_at)
                           VALUES (?,?,?,?,?,?,?)""",
                        (
                            new_id("re"),
                            rid,
                            stored_event_type,
                            reason,
                            event_actor,
                            dumps(edge_meta),
                            now,
                        ),
                    )
            else:
                meta = dict(base_meta)
                meta["previous_status"] = db_status
                meta["resulting_status"] = new_status
                meta["status"] = new_status
                meta["reason"] = event_summary
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
        mutation_type: str = "status_transition",
        transition_path: list[str] | None = None,
    ) -> dict[str, Any]:
        """Compatibility wrapper — delegates to commit_run_mutation authority."""
        if expected_row_version is None:
            expected_row_version = int(run.get("row_version") or 0) or None
        if expected_row_version is None:
            raise ValueError("transition requires expected_row_version")
        return self.commit_run_mutation(
            run,
            expected_row_version=int(expected_row_version),
            mutation_type=mutation_type,
            event_type=event_type,
            event_summary=event_summary,
            event_actor=event_actor,
            event_metadata=event_metadata,
            transition_path=transition_path,
        )

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
        """Append an audit event for an existing run only.

        Fail closed: missing run_id raises; never fabricates a placeholder run.
        """
        rid = str(run_id or "").strip()
        if not rid:
            raise ValueError("run_id required for run event")
        event = {
            "id": new_id("re"),
            "run_id": rid,
            "event_type": event_type,
            "summary": summary,
            "actor": actor,
            "metadata": metadata or {},
            "created_at": utc_now_iso(),
        }
        with self.db.transaction() as conn:
            exists = conn.execute("SELECT id FROM runs WHERE id=?", (rid,)).fetchone()
            if not exists:
                raise ValueError(
                    f"cannot append run event: run not found: {rid} "
                    "(refusing placeholder run fabrication)"
                )
            conn.execute(
                """INSERT INTO run_events(id, run_id, event_type, summary, actor, metadata_json, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    event["id"],
                    rid,
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

    # —— Stage 7 Packet 7B review packets/executions ——
    def save_review_packet_atomic(self, *, packet: dict[str, Any], actor: str) -> dict[str, Any]:
        from buildforme.review_execution import validate_review_packet_for_storage

        record = dict(packet)
        cycle_id = str(record.get("cycle_id") or "")
        assignment_id = str(record.get("assignment_id") or "")
        packet_id = str(record.get("packet_id") or "")
        with self.db.transaction() as conn:
            cycle_row = conn.execute(
                "SELECT run_id, payload_json, status FROM review_cycles WHERE id=?",
                (cycle_id,),
            ).fetchone()
            assignment_row = conn.execute(
                "SELECT payload_json, status, cycle_id FROM review_assignments WHERE id=?",
                (assignment_id,),
            ).fetchone()
            if not cycle_row or not assignment_row:
                raise ValueError("review packet cycle or assignment not found")
            if str(assignment_row[2]) != cycle_id:
                raise ValueError("review packet assignment cycle mismatch")
            if str(assignment_row[1]) != "pending":
                raise ValueError("review packet requires pending assignment")
            cycle = loads(cycle_row[1], {})
            cycle["status"] = cycle_row[2]
            assignment = loads(assignment_row[0], {})
            assignment["status"] = assignment_row[1]
            run_row = conn.execute(
                "SELECT payload_json FROM runs WHERE id=?", (str(cycle_row[0]),)
            ).fetchone()
            evidence_row = conn.execute(
                "SELECT payload_json FROM evidence WHERE evidence_id=?",
                (str(cycle.get("evidence_id") or ""),),
            ).fetchone()
            if not run_row or not evidence_row:
                raise ValueError("review packet run or evidence not found")
            run = loads(run_row[0], {})
            evidence = loads(evidence_row[0], {})
            problems = validate_review_packet_for_storage(
                record,
                cycle=cycle,
                assignment=assignment,
                run=run,
                evidence=evidence,
            )
            if problems:
                raise ValueError("review packet rejected: " + "; ".join(problems))
            existing = conn.execute(
                "SELECT payload_json, packet_fingerprint FROM review_packets WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if existing:
                prior = loads(existing[0], {})
                if str(existing[1] or "") != str(record.get("packet_fingerprint") or ""):
                    raise ValueError("review packet mutation forbidden")
                return prior
            conn.execute(
                "INSERT INTO review_packets(packet_id, cycle_id, assignment_id, packet_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,1)",
                (
                    packet_id,
                    cycle_id,
                    assignment_id,
                    record.get("packet_fingerprint"),
                    dumps(record),
                    record.get("created_at") or utc_now_iso(),
                ),
            )
            conn.execute(
                "INSERT INTO review_events(id, cycle_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    new_id("rve"),
                    cycle_id,
                    "review_packet_bound",
                    "Immutable blind-review packet bound to assignment",
                    actor,
                    dumps({"assignment_id": assignment_id, "packet_id": packet_id}),
                    utc_now_iso(),
                ),
            )
        return record

    def get_review_packet_for_assignment(self, assignment_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM review_packets WHERE assignment_id=?",
                (str(assignment_id),),
            ).fetchone()
        if not row:
            raise KeyError(f"Review packet not found for assignment: {assignment_id}")
        return loads(row[0], {})

    def list_review_execution_attempts(self, assignment_id: str) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM review_executions WHERE assignment_id=? ORDER BY created_at, execution_id",
                (str(assignment_id),),
            ).fetchall()
        return [loads(row[0], {}) for row in rows]

    def record_review_execution_atomic(self, *, execution: dict[str, Any], actor: str) -> dict[str, Any]:
        from buildforme.review_execution import validate_review_execution_record

        record = dict(execution)
        if str(record.get("status") or "") == "succeeded":
            raise ValueError("successful reviewer execution must commit atomically with its report")
        assignment_id = str(record.get("assignment_id") or "")
        with self.db.transaction() as conn:
            assignment_row = conn.execute(
                "SELECT payload_json FROM review_assignments WHERE id=?",
                (assignment_id,),
            ).fetchone()
            packet_row = conn.execute(
                "SELECT payload_json FROM review_packets WHERE packet_id=? AND assignment_id=?",
                (str(record.get("packet_id") or ""), assignment_id),
            ).fetchone()
            if not assignment_row or not packet_row:
                raise ValueError("review execution assignment or packet not found")
            assignment = loads(assignment_row[0], {})
            packet = loads(packet_row[0], {})
            problems = validate_review_execution_record(
                record, packet=packet, assignment=assignment, report=None
            )
            if problems:
                raise ValueError("review execution rejected: " + "; ".join(problems))
            if conn.execute(
                "SELECT execution_id FROM review_executions WHERE execution_id=?",
                (str(record.get("execution_id") or ""),),
            ).fetchone():
                raise ValueError("review execution evidence is append-only")
            conn.execute(
                "INSERT INTO review_executions(execution_id, cycle_id, assignment_id, packet_id, provider_id, status, execution_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,?,?,1)",
                (
                    record["execution_id"],
                    record["cycle_id"],
                    assignment_id,
                    record["packet_id"],
                    record["provider_id"],
                    record["status"],
                    record["execution_fingerprint"],
                    dumps(record),
                    record.get("created_at") or utc_now_iso(),
                ),
            )
            conn.execute(
                "INSERT INTO review_events(id, cycle_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    new_id("rve"),
                    record["cycle_id"],
                    "review_execution_failed",
                    "Automated reviewer execution failed closed",
                    actor,
                    dumps({"assignment_id": assignment_id, "execution_id": record["execution_id"]}),
                    utc_now_iso(),
                ),
            )
        return record

    # —— Stage 7 independent reviews ——
    def create_review_cycle_atomic(
        self,
        *,
        cycle: dict[str, Any],
        assignments: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        from buildforme.evidence import EVIDENCE_KIND_EXECUTION, validate_evidence_for_storage
        from buildforme.review_contracts import validate_assignment_record, validate_cycle_record

        cycle_record = dict(cycle)
        problems = validate_cycle_record(cycle_record)
        if problems:
            raise ValueError("review cycle rejected: " + "; ".join(problems))
        assignment_records = [dict(item) for item in assignments]
        for item in assignment_records:
            problems = validate_assignment_record(item, cycle_record)
            if problems:
                raise ValueError("review assignment rejected: " + "; ".join(problems))
        declared_reviewers = {
            (str(item.get("reviewer_id")), str(item.get("provider_id")), str(item.get("role")))
            for item in (cycle_record.get("reviewers") or [])
        }
        assigned_reviewers = {
            (str(item.get("reviewer_id")), str(item.get("provider_id")), str(item.get("role")))
            for item in assignment_records
        }
        if declared_reviewers != assigned_reviewers or len(assignment_records) != len(
            cycle_record.get("reviewers") or []
        ):
            raise ValueError("review assignments do not exactly match declared reviewers")
        cycle_id = str(cycle_record["cycle_id"])
        run_id = str(cycle_record["run_id"])
        now = utc_now_iso()
        with self.db.transaction() as conn:
            run_row = conn.execute(
                "SELECT row_version, payload_json FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if not run_row:
                raise KeyError(f"Run not found: {run_id}")
            run = loads(run_row[1], {})
            if str(run.get("status") or "") != "needs_review":
                raise ValueError("review cycle requires run status needs_review")
            if str(cycle_record.get("scope_fingerprint") or "") != str(
                run.get("scope_fingerprint") or ""
            ):
                raise ValueError("review cycle scope does not match canonical run")
            if str(cycle_record.get("constitution_hash") or "") != str(
                run.get("constitution_hash") or ""
            ):
                raise ValueError("review cycle Constitution does not match canonical run")
            if str(cycle_record.get("constitution_lease_id") or "") != str(
                run.get("constitution_lease_id") or ""
            ):
                raise ValueError("review cycle Constitution lease does not match canonical run")
            if str(cycle_record.get("implementer_provider_id") or "") != str(
                run.get("provider_id") or ""
            ):
                raise ValueError("review cycle implementer provider does not match canonical run")
            evidence_rows = conn.execute(
                "SELECT evidence_id, payload_json, evidence_fingerprint FROM evidence WHERE run_id=? ORDER BY sequence DESC",
                (run_id,),
            ).fetchall()
            latest_execution = None
            for candidate in evidence_rows:
                payload = loads(candidate[1], {})
                if str(payload.get("evidence_kind") or "") == EVIDENCE_KIND_EXECUTION:
                    latest_execution = (candidate, payload)
                    break
            if latest_execution is None:
                raise ValueError("latest execution evidence not found")
            evidence_row, evidence_payload = latest_execution
            if str(evidence_row[0]) != str(cycle_record["evidence_id"]):
                raise ValueError("review cycle must bind the latest execution evidence")
            evidence_problems = validate_evidence_for_storage(evidence_payload)
            if evidence_problems:
                raise ValueError("bound execution evidence invalid: " + "; ".join(evidence_problems))
            actual_evidence_fp = str(
                evidence_payload.get("evidence_fingerprint") or evidence_row[2] or ""
            )
            if actual_evidence_fp != str(cycle_record["evidence_fingerprint"]):
                raise ValueError("bound execution evidence fingerprint mismatch")
            evidence_constitution = (
                evidence_payload.get("constitution")
                if isinstance(evidence_payload.get("constitution"), dict)
                else {}
            )
            if str(evidence_constitution.get("hash") or "") != str(run.get("constitution_hash") or ""):
                raise ValueError("execution evidence Constitution is stale")
            prior_same_evidence = conn.execute(
                "SELECT id, status FROM review_cycles WHERE run_id=? AND evidence_id=? ORDER BY created_at DESC LIMIT 1",
                (run_id, str(cycle_record["evidence_id"])),
            ).fetchone()
            if prior_same_evidence:
                raise ValueError(
                    "execution evidence has already been independently reviewed; "
                    "a new cycle requires fresh repair and execution evidence"
                )
            if conn.execute(
                "SELECT id FROM review_cycles WHERE run_id=? AND status IN ('open','collecting','ready_to_aggregate')",
                (run_id,),
            ).fetchone():
                raise ValueError("an active independent review cycle already exists for this run")
            conn.execute(
                """INSERT INTO review_cycles(
                   id, run_id, evidence_id, evidence_fingerprint, scope_fingerprint,
                   constitution_hash, status, required_reviewer_count, min_distinct_providers,
                   policy_json, aggregate_json, payload_json, created_at, updated_at,
                   finalized_at, row_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,1)""",
                (
                    cycle_id,
                    run_id,
                    cycle_record["evidence_id"],
                    cycle_record["evidence_fingerprint"],
                    cycle_record["scope_fingerprint"],
                    cycle_record["constitution_hash"],
                    "open",
                    int(cycle_record["required_reviewer_count"]),
                    int(cycle_record["min_distinct_providers"]),
                    dumps(cycle_record.get("policy") or {}),
                    None,
                    dumps(cycle_record),
                    cycle_record.get("created_at") or now,
                    now,
                ),
            )
            for assignment in assignment_records:
                conn.execute(
                    """INSERT INTO review_assignments(
                       id, cycle_id, reviewer_id, provider_id, role, status, blind,
                       payload_json, created_at, submitted_at)
                       VALUES (?,?,?,?,?,'pending',1,?,?,NULL)""",
                    (
                        assignment["assignment_id"],
                        cycle_id,
                        assignment["reviewer_id"],
                        assignment["provider_id"],
                        assignment["role"],
                        dumps(assignment),
                        assignment.get("created_at") or now,
                    ),
                )
            run["stage7_review_required"] = True
            run["stage7_review_cycle_id"] = cycle_id
            run["independent_review"] = {
                "cycle_id": cycle_id,
                "status": "collecting",
                "quorum_met": False,
                "evidence_id": cycle_record["evidence_id"],
                "evidence_fingerprint": cycle_record["evidence_fingerprint"],
                "required_reviewer_count": cycle_record["required_reviewer_count"],
            }
            run["updated_at"] = now
            new_run_version = int(run_row[0] or 1) + 1
            run["row_version"] = new_run_version
            cur = conn.execute(
                "UPDATE runs SET payload_json=?, updated_at=?, row_version=? WHERE id=? AND row_version=?",
                (dumps(run), now, new_run_version, run_id, int(run_row[0] or 1)),
            )
            if cur.rowcount != 1:
                raise ValueError("stale run race while binding review cycle")
            conn.execute(
                "INSERT INTO review_events(id, cycle_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id("rve"), cycle_id, "review_cycle_created", "Blind independent review cycle created", actor, dumps({"assignment_count": len(assignment_records)}), now),
            )
            conn.execute(
                "INSERT INTO run_events(id, run_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id("re"), run_id, "stage7_review_cycle_created", "Stage 7 independent review required", actor, dumps({"cycle_id": cycle_id}), now),
            )
        return {"cycle": cycle_record, "assignments": assignment_records, "run": run}

    def get_review_cycle(self, cycle_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT payload_json, aggregate_json, status, row_version, finalized_at FROM review_cycles WHERE id=?",
                (str(cycle_id),),
            ).fetchone()
        if not row:
            raise KeyError(f"Review cycle not found: {cycle_id}")
        record = loads(row[0], {})
        record["status"] = row[2]
        record["row_version"] = int(row[3] or 1)
        record["finalized_at"] = row[4]
        record["aggregate"] = loads(row[1], None) if row[1] else record.get("aggregate")
        return record

    def list_review_cycles(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT id FROM review_cycles WHERE run_id=? ORDER BY created_at DESC",
                    (str(run_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id FROM review_cycles ORDER BY created_at DESC"
                ).fetchall()
        return [self.get_review_cycle(str(row[0])) for row in rows]

    def get_review_assignment(self, assignment_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT payload_json, status, submitted_at FROM review_assignments WHERE id=?",
                (str(assignment_id),),
            ).fetchone()
        if not row:
            raise KeyError(f"Review assignment not found: {assignment_id}")
        record = loads(row[0], {})
        record["status"] = row[1]
        record["submitted_at"] = row[2]
        return record

    def list_review_assignments(self, cycle_id: str) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT id FROM review_assignments WHERE cycle_id=? ORDER BY provider_id, reviewer_id",
                (str(cycle_id),),
            ).fetchall()
        return [self.get_review_assignment(str(row[0])) for row in rows]

    def list_review_reports(self, cycle_id: str) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM review_reports WHERE cycle_id=? ORDER BY created_at, report_id",
                (str(cycle_id),),
            ).fetchall()
        return [loads(row[0], {}) for row in rows]

    def list_review_findings(self, cycle_id: str) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM review_findings WHERE cycle_id=? ORDER BY created_at, finding_id",
                (str(cycle_id),),
            ).fetchall()
        return [loads(row[0], {}) for row in rows]

    def submit_review_report_atomic(
        self,
        *,
        cycle_id: str,
        assignment_id: str,
        report: dict[str, Any],
        findings: list[dict[str, Any]],
        actor: str,
        execution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from buildforme.review_contracts import (
            validate_finding_for_storage,
            validate_report_for_storage,
        )
        from buildforme.review_execution import validate_review_execution_record

        if not isinstance(execution, dict):
            raise ValueError("direct review report submission disabled; authenticated reviewer execution required")
        execution_record = dict(execution)
        now = utc_now_iso()
        with self.db.transaction() as conn:
            cycle_row = conn.execute(
                "SELECT run_id, status, required_reviewer_count, payload_json, row_version FROM review_cycles WHERE id=?",
                (str(cycle_id),),
            ).fetchone()
            if not cycle_row:
                raise KeyError(f"Review cycle not found: {cycle_id}")
            if str(cycle_row[1]) not in {"open", "collecting", "ready_to_aggregate"}:
                raise ValueError("review cycle is not accepting reports")
            cycle = loads(cycle_row[3], {})
            cycle["status"] = cycle_row[1]
            cycle["row_version"] = int(cycle_row[4] or 1)
            assignment_row = conn.execute(
                "SELECT cycle_id, status, payload_json FROM review_assignments WHERE id=?",
                (str(assignment_id),),
            ).fetchone()
            if not assignment_row:
                raise KeyError(f"Review assignment not found: {assignment_id}")
            if str(assignment_row[0]) != str(cycle_id):
                raise ValueError("review assignment cycle mismatch")
            if str(assignment_row[1]) != "pending":
                raise ValueError("review assignment already submitted or unavailable")
            assignment = loads(assignment_row[2], {})
            assignment["status"] = assignment_row[1]
            problems = validate_report_for_storage(report, cycle, assignment)
            if problems:
                raise ValueError("review report rejected: " + "; ".join(problems))
            packet_row = conn.execute(
                "SELECT payload_json FROM review_packets WHERE assignment_id=?",
                (str(assignment_id),),
            ).fetchone()
            if not packet_row:
                raise ValueError("authenticated reviewer execution requires immutable review packet")
            review_packet = loads(packet_row[0], {})
            execution_problems = validate_review_execution_record(
                execution_record,
                packet=review_packet,
                assignment=assignment,
                report=report,
            )
            if execution_problems:
                raise ValueError("review execution rejected: " + "; ".join(execution_problems))
            if str(execution_record.get("status") or "") != "succeeded":
                raise ValueError("review report requires successful reviewer execution")
            if conn.execute(
                "SELECT execution_id FROM review_executions WHERE execution_id=?",
                (str(execution_record.get("execution_id") or ""),),
            ).fetchone():
                raise ValueError("review execution evidence is append-only")
            report_findings = report.get("findings") if isinstance(report.get("findings"), list) else []
            if findings != report_findings:
                raise ValueError("separate review findings diverge from report findings")
            finding_ids: set[str] = set()
            for finding in findings:
                finding_problems = validate_finding_for_storage(
                    finding,
                    report=report,
                    cycle=cycle,
                    assignment=assignment,
                )
                if finding_problems:
                    raise ValueError("review finding rejected: " + "; ".join(finding_problems))
                finding_id = str(finding.get("finding_id") or "")
                if finding_id in finding_ids:
                    raise ValueError("duplicate finding id within review report")
                finding_ids.add(finding_id)
            report_id = str(report["report_id"])
            if conn.execute(
                "SELECT report_id FROM review_reports WHERE report_id=? OR assignment_id=?",
                (report_id, str(assignment_id)),
            ).fetchone():
                raise ValueError("review report is append-only and assignment may submit only once")
            conn.execute(
                "INSERT INTO review_executions(execution_id, cycle_id, assignment_id, packet_id, provider_id, status, execution_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,?,?,1)",
                (
                    execution_record["execution_id"],
                    execution_record["cycle_id"],
                    execution_record["assignment_id"],
                    execution_record["packet_id"],
                    execution_record["provider_id"],
                    execution_record["status"],
                    execution_record["execution_fingerprint"],
                    dumps(execution_record),
                    execution_record.get("created_at") or now,
                ),
            )
            conn.execute(
                "INSERT INTO review_reports(report_id, cycle_id, assignment_id, verdict, report_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,1)",
                (report_id, cycle_id, assignment_id, report["verdict"], report["report_fingerprint"], dumps(report), report.get("created_at") or now),
            )
            for finding in findings:
                finding_id = str(finding["finding_id"])
                if conn.execute(
                    "SELECT finding_id FROM review_findings WHERE finding_id=?", (finding_id,)
                ).fetchone():
                    raise ValueError(f"review finding mutation forbidden: {finding_id}")
                conn.execute(
                    "INSERT INTO review_findings(finding_id, report_id, cycle_id, assignment_id, severity, category, blocking, finding_fingerprint, payload_json, created_at, immutable) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                    (finding_id, report_id, cycle_id, assignment_id, finding["severity"], finding["category"], 1 if finding.get("blocking") else 0, finding["finding_fingerprint"], dumps(finding), now),
                )
            assignment["status"] = "submitted"
            assignment["submitted_at"] = now
            assignment["report_id"] = report_id
            assignment["report_fingerprint"] = report["report_fingerprint"]
            conn.execute(
                "UPDATE review_assignments SET status='submitted', payload_json=?, submitted_at=? WHERE id=? AND status='pending'",
                (dumps(assignment), now, assignment_id),
            )
            submitted = int(
                conn.execute(
                    "SELECT COUNT(*) FROM review_assignments WHERE cycle_id=? AND status='submitted'",
                    (cycle_id,),
                ).fetchone()[0]
            )
            required = int(cycle_row[2] or 0)
            new_status = "ready_to_aggregate" if submitted >= required else "collecting"
            cycle["status"] = new_status
            cycle["submitted_reviewer_count"] = submitted
            cycle["updated_at"] = now
            new_cycle_version = int(cycle_row[4] or 1) + 1
            cycle["row_version"] = new_cycle_version
            cur = conn.execute(
                "UPDATE review_cycles SET status=?, payload_json=?, updated_at=?, row_version=? WHERE id=? AND row_version=?",
                (new_status, dumps(cycle), now, new_cycle_version, cycle_id, int(cycle_row[4] or 1)),
            )
            if cur.rowcount != 1:
                raise ValueError("stale review cycle race while submitting report")
            conn.execute(
                "INSERT INTO review_events(id, cycle_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id("rve"), cycle_id, "review_report_submitted", "Blind reviewer report submitted", actor, dumps({"assignment_id": assignment_id, "report_id": report_id, "execution_id": execution_record.get("execution_id"), "submitted": submitted, "required": required}), now),
            )
            conn.execute(
                "INSERT INTO run_events(id, run_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id("re"), str(cycle_row[0]), "stage7_review_report_submitted", "Independent reviewer report submitted", actor, dumps({"cycle_id": cycle_id, "assignment_id": assignment_id, "report_id": report_id}), now),
            )
        return {"cycle": cycle, "assignment": assignment, "report": report, "findings": findings}

    def finalize_review_cycle_atomic(
        self,
        *,
        cycle_id: str,
        expected_row_version: int,
        aggregate: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        from buildforme.review_contracts import aggregate_review_reports

        now = utc_now_iso()
        with self.db.transaction() as conn:
            cycle_row = conn.execute(
                "SELECT run_id, status, payload_json, row_version FROM review_cycles WHERE id=?",
                (str(cycle_id),),
            ).fetchone()
            if not cycle_row:
                raise KeyError(f"Review cycle not found: {cycle_id}")
            current_version = int(cycle_row[3] or 1)
            if current_version != int(expected_row_version):
                raise ValueError("stale review cycle aggregation rejected")
            if str(cycle_row[1]) != "ready_to_aggregate":
                raise ValueError("review cycle is not ready to aggregate")
            cycle = loads(cycle_row[2], {})
            cycle["status"] = cycle_row[1]
            cycle["row_version"] = current_version
            assignment_rows = conn.execute(
                "SELECT payload_json, status FROM review_assignments WHERE cycle_id=? ORDER BY provider_id, reviewer_id",
                (cycle_id,),
            ).fetchall()
            assignments = []
            for row in assignment_rows:
                item = loads(row[0], {})
                item["status"] = row[1]
                assignments.append(item)
            reports = [
                loads(row[0], {})
                for row in conn.execute(
                    "SELECT payload_json FROM review_reports WHERE cycle_id=? ORDER BY created_at, report_id",
                    (cycle_id,),
                ).fetchall()
            ]
            canonical = aggregate_review_reports(cycle=cycle, assignments=assignments, reports=reports)
            if canonical.get("aggregate_fingerprint") != aggregate.get("aggregate_fingerprint"):
                raise ValueError("review aggregate fingerprint mismatch")
            if canonical.get("status") != aggregate.get("status"):
                raise ValueError("review aggregate status mismatch")
            final_status = str(canonical["status"])
            cycle["status"] = final_status
            cycle["aggregate"] = canonical
            cycle["updated_at"] = now
            cycle["finalized_at"] = now
            new_cycle_version = current_version + 1
            cycle["row_version"] = new_cycle_version
            cur = conn.execute(
                "UPDATE review_cycles SET status=?, aggregate_json=?, payload_json=?, updated_at=?, finalized_at=?, row_version=? WHERE id=? AND row_version=?",
                (final_status, dumps(canonical), dumps(cycle), now, now, new_cycle_version, cycle_id, current_version),
            )
            if cur.rowcount != 1:
                raise ValueError("stale review cycle finalization race")
            run_id = str(cycle_row[0])
            run_row = conn.execute(
                "SELECT row_version, payload_json FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if not run_row:
                raise KeyError(f"Run not found: {run_id}")
            run = loads(run_row[1], {})
            if str(run.get("status") or "") != "needs_review":
                raise ValueError("review cycle can finalize only while run needs_review")
            if str(run.get("stage7_review_cycle_id") or "") != str(cycle_id):
                raise ValueError("run is not bound to this review cycle")
            run["independent_review"] = {
                "cycle_id": cycle_id,
                "status": final_status,
                "quorum_met": canonical.get("quorum_met"),
                "evidence_id": cycle.get("evidence_id"),
                "evidence_fingerprint": cycle.get("evidence_fingerprint"),
                "aggregate_fingerprint": canonical.get("aggregate_fingerprint"),
                "required_reviewer_count": canonical.get("required_reviewer_count"),
                "submitted_reviewer_count": canonical.get("submitted_reviewer_count"),
                "distinct_provider_count": canonical.get("distinct_provider_count"),
                "blocking_finding_count": canonical.get("blocking_finding_count"),
                "finding_count": canonical.get("finding_count"),
                "disagreement": canonical.get("disagreement"),
            }
            run["updated_at"] = now
            run_version = int(run_row[0] or 1)
            run["row_version"] = run_version + 1
            run_cur = conn.execute(
                "UPDATE runs SET payload_json=?, updated_at=?, row_version=? WHERE id=? AND row_version=?",
                (dumps(run), now, run_version + 1, run_id, run_version),
            )
            if run_cur.rowcount != 1:
                raise ValueError("stale run race while binding review aggregate")
            conn.execute(
                "INSERT INTO review_events(id, cycle_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id("rve"), cycle_id, "review_cycle_finalized", f"Independent review verdict: {final_status}", actor, dumps({"aggregate_fingerprint": canonical.get("aggregate_fingerprint")}), now),
            )
            conn.execute(
                "INSERT INTO run_events(id, run_id, event_type, summary, actor, metadata_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id("re"), run_id, "stage7_review_finalized", f"Independent review verdict: {final_status}", actor, dumps({"cycle_id": cycle_id, "aggregate_fingerprint": canonical.get("aggregate_fingerprint")}), now),
            )
        return {"cycle": cycle, "aggregate": canonical, "run": run}

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
        rid = str(record.get("run_id") or "").strip()
        if not rid:
            raise ValueError("evidence run_id required (refusing placeholder run fabrication)")
        with self.db.transaction() as conn:
            # Fail closed: run must already exist — never invent unknown/unknown shells
            if not conn.execute("SELECT id FROM runs WHERE id=?", (rid,)).fetchone():
                raise ValueError(
                    f"cannot save evidence: run not found: {rid} "
                    "(refusing placeholder run fabrication)"
                )
            exists = conn.execute("SELECT evidence_id FROM evidence WHERE evidence_id=?", (eid,)).fetchone()
            if exists:
                raise ValueError(f"evidence mutation forbidden: {eid} is append-only")
            # Parent evidence (if declared) must belong to this run
            parent_id = record.get("parent_evidence_id")
            if parent_id:
                parent_row = conn.execute(
                    "SELECT run_id FROM evidence WHERE evidence_id=?",
                    (str(parent_id),),
                ).fetchone()
                if not parent_row:
                    raise ValueError(f"parent evidence not found: {parent_id}")
                if str(parent_row[0] or "") != rid:
                    raise ValueError(
                        f"parent evidence {parent_id} belongs to run {parent_row[0]!r}, not {rid!r}"
                    )
            prior = conn.execute(
                "SELECT COUNT(*) FROM evidence WHERE run_id=?", (rid,)
            ).fetchone()[0]
            record.setdefault("sequence", int(prior) + 1)
            record.setdefault("attempt", record.get("attempt") or record["sequence"])
            if prior and not record.get("parent_evidence_id"):
                parent = conn.execute(
                    "SELECT evidence_id FROM evidence WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
                    (rid,),
                ).fetchone()
                if parent:
                    record.setdefault("parent_evidence_id", parent[0])
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
        if dry_run:
            return self._migrate_from_json_atomically_locked(
                runtime_dir,
                dry_run=True,
                cutover=False,
            )
        with self.db.maintenance_lock(timeout_seconds=120.0):
            return self._migrate_from_json_atomically_locked(
                runtime_dir,
                dry_run=False,
                cutover=cutover,
            )

    def _migrate_from_json_atomically_locked(
        self,
        runtime_dir: Path,
        *,
        dry_run: bool = False,
        cutover: bool = True,
    ) -> dict[str, Any]:
        """Import through a temporary SQLite authority and atomically replace on success.

        Any malformed record, orphan, integrity failure, or cutover failure leaves the
        original database logically unchanged.  Active runs block migration.
        """
        import os
        import sqlite3
        import uuid

        if dry_run:
            return self._migrate_from_json_in_place(
                runtime_dir, dry_run=True, cutover=False
            )

        conn = self.db._raw_connect()
        try:
            active = int(
                conn.execute(
                    "SELECT COUNT(*) FROM runs WHERE status IN ('queued','starting','running','cancel_requested')"
                ).fetchone()[0]
            )
        finally:
            conn.close()
        if active:
            return {
                "errors": [f"migration refused while {active} active run(s) exist"],
                "orphans": [],
                "malformed": [],
                "dry_run": False,
                "cutover": False,
                "rolled_back": True,
                "atomic_commit": False,
            }

        original = Path(self.db.path)
        temp_path = original.with_name(
            f".{original.name}.migration-{uuid.uuid4().hex}.tmp"
        )
        report: dict[str, Any] = {
            "errors": [],
            "orphans": [],
            "malformed": [],
            "dry_run": False,
            "cutover": False,
            "rolled_back": True,
            "atomic_commit": False,
        }
        temp_store: Stage6Store | None = None
        try:
            source = self.db._raw_connect()
            target = sqlite3.connect(str(temp_path))
            try:
                source.execute("PRAGMA wal_checkpoint(FULL)")
                source.backup(target)
                target.commit()
            finally:
                target.close()
                source.close()

            temp_store = Stage6Store(temp_path)
            report = temp_store._migrate_from_json_in_place(
                runtime_dir, dry_run=False, cutover=cutover
            )
            integrity_ok = str(report.get("integrity") or "").lower() == "ok"
            valid = (
                not report.get("errors")
                and not report.get("orphans")
                and integrity_ok
                and (not cutover or bool(report.get("cutover")))
            )
            if not valid:
                report["rolled_back"] = True
                report["atomic_commit"] = False
                return report

            check = temp_store.db._raw_connect()
            try:
                check.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
                if str(integrity).lower() != "ok":
                    raise ValueError(f"temporary migration database integrity failure: {integrity}")
            finally:
                check.close()

            for suffix in ("-wal", "-shm"):
                Path(str(original) + suffix).unlink(missing_ok=True)
                Path(str(temp_path) + suffix).unlink(missing_ok=True)
            os.replace(temp_path, original)
            report["rolled_back"] = False
            report["atomic_commit"] = True
            report["database_replaced_atomically"] = True
            return report
        except Exception as exc:
            report.setdefault("errors", []).append(f"atomic migration failed: {exc}")
            report["rolled_back"] = True
            report["atomic_commit"] = False
            return report
        finally:
            temp_path.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(temp_path) + suffix).unlink(missing_ok=True)

    def _migrate_from_json_in_place(
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
            "orphans": [],
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

        runs_src = _load("runs.json", "runs")
        events_src = _load("run_events.json", "events")
        approvals_src = _load("run_approvals.json", "approvals")
        evidence_src = _load("run_evidence.json", "evidence")
        leases_src = _load("constitution_leases.json", "leases")
        known_run_ids = {
            str(r.get("id"))
            for r in runs_src
            if isinstance(r, dict) and r.get("id")
        }

        def _orphan(record_type: str, record_id: str, run_id: str, source: str, reason: str) -> None:
            report["orphans"].append(
                {
                    "record_type": record_type,
                    "record_id": record_id,
                    "referenced_run_id": run_id,
                    "source_file": source,
                    "reason": reason,
                }
            )

        for ev in events_src:
            rid = str(ev.get("run_id") or "")
            eid = str(ev.get("id") or ev.get("event_id") or "")
            if not rid:
                report["malformed"].append("run_event missing run_id")
                continue
            if rid not in known_run_ids:
                _orphan("event", eid or "(no-id)", rid, "run_events.json", "referenced run not in runs.json")
        for ap in approvals_src:
            rid = str(ap.get("run_id") or "")
            aid = str(ap.get("id") or ap.get("approval_id") or "")
            if not rid:
                report["malformed"].append("approval missing run_id")
                continue
            if rid not in known_run_ids:
                _orphan(
                    "approval",
                    aid or "(no-id)",
                    rid,
                    "run_approvals.json",
                    "referenced run not in runs.json",
                )
        for ev in evidence_src:
            rid = str(ev.get("run_id") or "")
            evid = str(ev.get("evidence_id") or ev.get("id") or "")
            if not rid:
                report["malformed"].append("evidence missing run_id")
                continue
            if rid not in known_run_ids:
                _orphan(
                    "evidence",
                    evid or "(no-id)",
                    rid,
                    "run_evidence.json",
                    "referenced run not in runs.json",
                )
        for lease in leases_src:
            rid = str(lease.get("run_id") or "")
            if rid and rid not in known_run_ids:
                _orphan(
                    "lease",
                    str(lease.get("lease_id") or "(no-id)"),
                    rid,
                    "constitution_leases.json",
                    "referenced run not in runs.json",
                )

        if dry_run:
            report["preview"] = {
                "runs": len(runs_src),
                "leases": len(leases_src),
                "bindings": len(_load("repository_bindings.json", "bindings")),
                "events": len(events_src),
                "approvals": len(approvals_src),
                "task_locks": len(_load("task_locks.json", "locks")),
                "repository_locks": len(_load("repository_locks.json", "locks")),
                "evidence": len(evidence_src),
                "project_controls": len(_load("project_execution_controls.json", "controls")),
                "orphans": len(report["orphans"]),
            }
            report["db"] = self.db.pragmas()
            return report

        # Runs first (authority shells)
        count = 0
        for run in runs_src:
            if run.get("id"):
                try:
                    self.save_run(run, allow_unversioned=True)
                    count += 1
                except Exception as exc:
                    report["errors"].append(f"run {run.get('id')}: {exc}")
        report["imported"]["runs"] = count

        # Events — only for existing runs; never fabricate placeholders
        count = 0
        for ev in events_src:
            rid = str(ev.get("run_id") or "")
            if not rid:
                continue
            if rid not in known_run_ids:
                continue  # already recorded as orphan; not authoritative
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
        for ap in approvals_src:
            rid = str(ap.get("run_id") or "")
            if not (rid and ap.get("requirement_type")):
                continue
            if rid not in known_run_ids:
                continue
            try:
                self.save_run_approval(ap)
                count += 1
            except Exception as exc:
                report["errors"].append(f"approval: {exc}")
        report["imported"]["approvals"] = count

        # Leases (run_id optional for some leases)
        count = 0
        for lease in leases_src:
            if lease.get("lease_id"):
                rid = str(lease.get("run_id") or "")
                if rid and rid not in known_run_ids:
                    continue
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

        # Evidence — only for existing runs; never fabricate placeholders
        count = 0
        for ev in evidence_src:
            rid = str(ev.get("run_id") or "")
            if not rid or rid not in known_run_ids:
                continue
            try:
                if "evidence_id" in ev:
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
        # Post-condition: no unknown/unknown placeholder runs
        with self.db.transaction() as conn:
            placeholders = conn.execute(
                "SELECT id FROM runs WHERE repository=? OR project_id=?",
                ("unknown/unknown", "unknown"),
            ).fetchall()
        if placeholders:
            report["errors"].append(
                f"placeholder runs present after migration: {[p[0] for p in placeholders]}"
            )
        if cutover and not report["errors"] and not report["orphans"]:
            self.set_migration_cutover(f"sqlite_authority_{stamp}")
            report["cutover"] = True
            report["cutover_marker"] = self.get_migration_cutover()
        elif cutover:
            report["cutover"] = False
            if report["orphans"]:
                report["errors"].append(
                    f"cutover withheld because {len(report['orphans'])} orphan reference(s)"
                )
            elif report["errors"]:
                report["errors"].append("cutover withheld because import reported errors")
        return report
