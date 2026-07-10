from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] if Path(__file__).resolve().parent.name == "scripts" else Path.cwd()


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


store_path = ROOT / "buildforme" / "execution_store.py"
store_text = store_path.read_text(encoding="utf-8")

constants_start = store_text.index("# Always permitted bookkeeping keys on any mutation (not authority).")
constants_end = store_text.index("\ndef _values_equal", constants_start)
new_constants = '''# Lifecycle fields are storage-owned. Runtime callers may propose a target status,
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
'''
store_text = store_text[:constants_start] + new_constants + store_text[constants_end:]

method_start = store_text.index("    def commit_run_mutation(\n")
method_end = store_text.index("\n    def admit_run_atomic(\n", method_start)
new_method = '''    def commit_run_mutation(
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

            base_meta = dict(event_metadata or {})
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
'''
store_text = store_text[:method_start] + new_method + store_text[method_end:]

needle = '        "requested_target_branch",\n        "execution_branch",\n'
replacement = '        "requested_target_branch",\n        "target_branch",\n        "execution_branch",\n'
store_text = replace_once(store_text, needle, replacement, label="target_branch protection")
store_path.write_text(store_text, encoding="utf-8")

service_path = ROOT / "buildforme" / "execution_service.py"
service_text = service_path.read_text(encoding="utf-8")

reload_block = '''def _reload(store: LocalStore, run_id: str) -> dict[str, Any]:
    return store.get_run(run_id)


'''
reload_replacement = '''def _reload(store: LocalStore, run_id: str) -> dict[str, Any]:
    return store.get_run(run_id)


def _require_bound_scope(run: dict[str, Any]) -> str:
    packet = run.get("packet") if isinstance(run.get("packet"), dict) else None
    stored = str(run.get("scope_fingerprint") or "")
    if not stored:
        raise ValueError(
            "run missing governed scope_fingerprint; recreate through constitutional admission"
        )
    computed = compute_run_scope_fingerprint(run, packet)
    if computed != stored:
        raise ValueError(
            "run scope fingerprint mismatch; bound authority changed and requires a new run"
        )
    return computed


'''
service_text = replace_once(service_text, reload_block, reload_replacement, label="scope helper insertion")

terminal_block = '''    if is_terminal(str(run.get("status"))):
        raise ValueError("cannot preflight terminal run")
    status = str(run.get("status"))
'''
terminal_replacement = '''    if is_terminal(str(run.get("status"))):
        raise ValueError("cannot preflight terminal run")
    _require_bound_scope(run)
    status = str(run.get("status"))
'''
service_text = replace_once(service_text, terminal_block, terminal_replacement, label="preflight initial scope check")

scope_assignment = '''    result = evaluate_run_preflight(run, store)
    run = store.get_run(run_id)
    run["preflight"] = result
    run["approval_requirements"] = list(result.get("required_approvals") or [])
    run["scope_fingerprint"] = compute_run_scope_fingerprint(
        run,
        run.get("packet") if isinstance(run.get("packet"), dict) else None,
    )
    run["updated_at"] = utc_now_iso()
'''
scope_replacement = '''    result = evaluate_run_preflight(run, store)
    run = store.get_run(run_id)
    _require_bound_scope(run)
    run["preflight"] = result
    run["approval_requirements"] = list(result.get("required_approvals") or [])
    run["updated_at"] = utc_now_iso()
'''
service_text = replace_once(service_text, scope_assignment, scope_replacement, label="preflight scope rewrite removal")

dry_run_block = '''    run_id = validate_safe_id(run_id, field="run_id")
    run = store.get_run(run_id)
    if str(run.get("status")) not in {"approved", "queued"}:
'''
dry_run_replacement = '''    run_id = validate_safe_id(run_id, field="run_id")
    run = store.get_run(run_id)
    _require_bound_scope(run)
    if str(run.get("status")) not in {"approved", "queued"}:
'''
service_text = replace_once(service_text, dry_run_block, dry_run_replacement, label="dry-run scope check")
service_path.write_text(service_text, encoding="utf-8")

test_path = ROOT / "tests" / "test_run_mutation_authority_hardening.py"
test_path.write_text('''"""Packet 5A hardening: immutable scope and storage-owned lifecycle truth."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from buildforme.storage import LocalStore


class RunMutationAuthorityHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name) / "state.json")

    def _make_run(self, run_id: str, status: str = "running", **extra):
        payload = {
            "id": run_id,
            "project_id": "p",
            "task_id": "t",
            "packet_id": "pk",
            "provider_id": "codex",
            "repository": "owner/repo",
            "repository_local_path": "/repo",
            "baseline_ref": "HEAD",
            "baseline_commit": "b" * 40,
            "requested_target_branch": "feature/x",
            "target_branch": "feature/x-run",
            "execution_branch": "feature/x-run",
            "operating_mode": "IMPLEMENTATION",
            "risk": "YELLOW",
            "execution_mode": "live_supervised",
            "mode": "live_supervised",
            "transport": "cli",
            "requested_capabilities": ["read_repository"],
            "scope_fingerprint": "scope-1",
            "constitution_version": "1.0.0",
            "constitution_hash": "c" * 64,
            "constitution_lease_id": "lease-1",
            "constitution_lease_fingerprint": "lease-fp",
            "task_lock_id": "lock-1",
            "status": status,
            "status_history": [],
            "started_at": "2026-01-01T00:00:00+00:00" if status == "running" else None,
            "finished_at": None,
        }
        payload.update(extra)
        return self.store.save_run_for_setup(payload)

    def _commit(self, run, *, mutation_type, event_type="test", **kwargs):
        return self.store.commit_run_mutation(
            run,
            expected_row_version=int(run["row_version"]),
            mutation_type=mutation_type,
            event_type=event_type,
            event_summary=kwargs.pop("event_summary", "test mutation"),
            event_actor=kwargs.pop("event_actor", "system"),
            **kwargs,
        )

    def test_preflight_cannot_rewrite_bound_scope_same_state(self):
        self._make_run("run-scope-same", status="awaiting_preflight")
        run = self.store.get_run("run-scope-same")
        version = run["row_version"]
        run["preflight"] = {"passed": True}
        run["approval_requirements"] = ["shan_task_approval"]
        run["scope_fingerprint"] = "evil-scope"
        with self.assertRaisesRegex(ValueError, "authority field mutation forbidden: scope_fingerprint"):
            self._commit(run, mutation_type="preflight_result", event_type="preflight_passed")
        stored = self.store.get_run("run-scope-same")
        self.assertEqual(stored["scope_fingerprint"], "scope-1")
        self.assertEqual(stored["row_version"], version)
        self.assertEqual(self.store.list_run_events("run-scope-same"), [])

    def test_preflight_cannot_rewrite_bound_scope_during_transition(self):
        self._make_run("run-scope-edge", status="awaiting_preflight")
        run = self.store.get_run("run-scope-edge")
        version = run["row_version"]
        run["status"] = "awaiting_approval"
        run["preflight"] = {"passed": True}
        run["approval_requirements"] = ["shan_task_approval"]
        run["scope_fingerprint"] = "evil-scope"
        with self.assertRaisesRegex(ValueError, "authority field mutation forbidden: scope_fingerprint"):
            self._commit(run, mutation_type="preflight_result", event_type="preflight_passed")
        stored = self.store.get_run("run-scope-edge")
        self.assertEqual(stored["status"], "awaiting_preflight")
        self.assertEqual(stored["row_version"], version)
        self.assertEqual(self.store.list_run_events("run-scope-edge"), [])

    def test_missing_legacy_scope_is_not_runtime_backfilled(self):
        self._make_run("run-no-scope", status="awaiting_preflight", scope_fingerprint=None)
        run = self.store.get_run("run-no-scope")
        run["scope_fingerprint"] = "new-runtime-scope"
        run["preflight"] = {"passed": True}
        with self.assertRaisesRegex(ValueError, "authority field mutation forbidden: scope_fingerprint"):
            self._commit(run, mutation_type="preflight_result")
        self.assertIsNone(self.store.get_run("run-no-scope").get("scope_fingerprint"))

    def test_same_state_metadata_cannot_fabricate_lifecycle_truth(self):
        self._make_run(
            "run-history",
            status="running",
            status_history=[
                {
                    "from": "starting",
                    "to": "running",
                    "actor": "system",
                    "reason": "real",
                    "at": "2026-01-01T00:00:00+00:00",
                }
            ],
            started_at="2026-01-01T00:00:00+00:00",
        )
        run = self.store.get_run("run-history")
        original_history = list(run["status_history"])
        original_started = run["started_at"]
        run["status_history"] = [{"from": "draft", "to": "completed", "actor": "attacker"}]
        run["started_at"] = "2099-01-01T00:00:00+00:00"
        run["finished_at"] = "2099-01-01T00:00:00+00:00"
        run["process_result"] = {"ok": True, "exit_code": 0}
        saved = self._commit(run, mutation_type="process_result", event_type="process_snapshot")
        self.assertEqual(saved["status_history"], original_history)
        self.assertEqual(saved["started_at"], original_started)
        self.assertIsNone(saved["finished_at"])
        self.assertTrue(saved["process_result"]["ok"])

    def test_explicit_path_derives_history_and_matching_events(self):
        self._make_run("run-path-history", status="approved", started_at=None)
        run = self.store.get_run("run-path-history")
        run["status"] = "running"
        run["worktree_path"] = "/tmp/wt"
        saved = self._commit(
            run,
            mutation_type="process_started",
            event_type="supervised_started",
            event_summary="provider launched",
            event_actor="system",
            transition_path=["approved", "queued", "starting", "running"],
        )
        history = saved["status_history"]
        events = self.store.list_run_events("run-path-history")
        self.assertEqual([(h["from"], h["to"]) for h in history], [
            ("approved", "queued"),
            ("queued", "starting"),
            ("starting", "running"),
        ])
        self.assertEqual(
            [(e["metadata"]["previous_status"], e["metadata"]["resulting_status"]) for e in events],
            [(h["from"], h["to"]) for h in history],
        )
        self.assertTrue(saved["started_at"])
        self.assertIsNone(saved["finished_at"])
        for history_entry, event in zip(history, events, strict=True):
            self.assertEqual(history_entry["actor"], event["actor"])
            self.assertEqual(history_entry["at"], event["created_at"])
            self.assertEqual(history_entry["reason"], event["summary"])
            self.assertEqual(event["metadata"]["timestamp"], event["created_at"])

    def test_terminal_path_derives_finished_at(self):
        self._make_run("run-terminal-path", status="running")
        run = self.store.get_run("run-terminal-path")
        run["status"] = "completed"
        run["dry_run_result"] = {"ok": True}
        saved = self._commit(
            run,
            mutation_type="dry_run_finished",
            event_type="dry_run_completed",
            transition_path=["running", "needs_review", "completed"],
        )
        self.assertEqual(saved["status"], "completed")
        self.assertTrue(saved["finished_at"])
        self.assertEqual(len(saved["status_history"]), 2)

    def test_invalid_path_rolls_back_history_timestamps_version_and_events(self):
        self._make_run("run-rollback", status="approved", started_at=None)
        before = self.store.get_run("run-rollback")
        proposed = dict(before)
        proposed["status"] = "needs_review"
        proposed["worktree_path"] = "/tmp/wt"
        with self.assertRaisesRegex(ValueError, "invalid transition edge"):
            self._commit(
                proposed,
                mutation_type="process_started",
                transition_path=["approved", "running", "needs_review"],
            )
        after = self.store.get_run("run-rollback")
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["status_history"], before["status_history"])
        self.assertEqual(after["started_at"], before["started_at"])
        self.assertEqual(after["finished_at"], before["finished_at"])
        self.assertEqual(after["row_version"], before["row_version"])
        self.assertEqual(self.store.list_run_events("run-rollback"), [])

    def test_same_state_transition_path_is_rejected(self):
        self._make_run("run-same-path", status="running")
        run = self.store.get_run("run-same-path")
        run["process_result"] = {"ok": True}
        with self.assertRaisesRegex(ValueError, "same-state"):
            self._commit(
                run,
                mutation_type="process_result",
                transition_path=["running", "needs_review", "running"],
            )

    def test_positive_verification_evidence_and_review_mutations(self):
        self._make_run("run-positive", status="running")
        run = self.store.get_run("run-positive")
        run["verification"] = {"passed": True}
        run = self._commit(run, mutation_type="verification_result", event_type="verified")
        self.assertTrue(run["verification"]["passed"])

        run["evidence"] = {"evidence_id": "ev-1"}
        run["evidence_ids"] = ["ev-1"]
        run["final_head_sha"] = "f" * 40
        run["head_commit"] = "f" * 40
        run = self._commit(run, mutation_type="execution_evidence_link", event_type="evidence_linked")
        self.assertEqual(run["evidence"]["evidence_id"], "ev-1")

        run["review"] = {"status": "review_required"}
        run["result_summary"] = "ready"
        run = self._commit(run, mutation_type="review_package", event_type="review_ready")
        self.assertEqual(run["review"]["status"], "review_required")

    def test_storage_status_policy_cannot_be_broadened_by_caller(self):
        self._make_run("run-policy", status="draft", started_at=None)
        run = self.store.get_run("run-policy")
        run["worktree_path"] = "/tmp/forbidden"
        with self.assertRaisesRegex(ValueError, "not permitted from status"):
            self._commit(
                run,
                mutation_type="process_started",
                require_db_status_in={"draft"},
            )
        self.assertIsNone(self.store.get_run("run-policy").get("worktree_path"))

    def test_all_runtime_modules_forbid_setup_and_unrestricted_save_apis(self):
        modules = [
            "buildforme/execution_service.py",
            "buildforme/server.py",
            "buildforme/review_gate.py",
            "buildforme/process_supervisor.py",
        ]
        forbidden = [
            re.compile(r"\bsave_run_for_setup\b"),
            re.compile(r"allow_unversioned\s*=\s*True"),
            re.compile(r"(?:store|self\._store\(\)|self\.s6|store\.s6)\.save_run\s*\("),
        ]
        for relative in modules:
            source = Path(relative).read_text(encoding="utf-8")
            for pattern in forbidden:
                self.assertIsNone(
                    pattern.search(source),
                    msg=f"{relative} contains forbidden runtime write API: {pattern.pattern}",
                )


if __name__ == "__main__":
    unittest.main()
''', encoding="utf-8")

print("Packet 5A hardening applied")
