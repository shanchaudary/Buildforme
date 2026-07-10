from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


service_path = ROOT / "buildforme" / "execution_service.py"
service = service_path.read_text(encoding="utf-8")
service = replace_once(
    service,
    '''    run_id = validate_safe_id(run_id, field="run_id")
    run = store.get_run(run_id)
    _require_canonical_run_lease(store, run)
    actor = validate_actor(actor)
''',
    '''    run_id = validate_safe_id(run_id, field="run_id")
    run = store.get_run(run_id)
    _require_bound_scope(run)
    _require_canonical_run_lease(store, run)
    actor = validate_actor(actor)
''',
    label="approval scope entrypoint",
)
service = replace_once(
    service,
    '''    run_id = validate_safe_id(run_id, field="run_id")
    run = store.get_run(run_id)
    if str(run.get("execution_mode") or run.get("mode") or "dry_run") != "live_supervised":
        raise ValueError("run execution_mode must be live_supervised (use run-dry-run for dry_run)")
    if str(run.get("status")) not in {"approved", "queued"}:
''',
    '''    run_id = validate_safe_id(run_id, field="run_id")
    run = store.get_run(run_id)
    if str(run.get("execution_mode") or run.get("mode") or "dry_run") != "live_supervised":
        raise ValueError("run execution_mode must be live_supervised (use run-dry-run for dry_run)")
    _require_bound_scope(run)
    if str(run.get("status")) not in {"approved", "queued"}:
''',
    label="supervised execution scope entrypoint",
)
service_path.write_text(service, encoding="utf-8")


test_path = ROOT / "tests" / "test_run_mutation_authority_hardening.py"
tests = test_path.read_text(encoding="utf-8")
tests = replace_once(
    tests,
    '''from buildforme.storage import LocalStore
''',
    '''from buildforme.execution_service import execute_supervised, record_run_approval
from buildforme.storage import LocalStore
''',
    label="entrypoint test imports",
)
insert_before = '''

if __name__ == "__main__":
    unittest.main()
'''
new_tests = '''

    def test_live_execution_rejects_missing_bound_scope_before_other_authority(self):
        self._make_run(
            "run-live-missing-scope",
            status="approved",
            scope_fingerprint=None,
        )
        before = self.store.get_run("run-live-missing-scope")
        with self.assertRaisesRegex(ValueError, "missing governed scope_fingerprint"):
            execute_supervised(self.store, "run-live-missing-scope")
        after = self.store.get_run("run-live-missing-scope")
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["row_version"], before["row_version"])
        self.assertEqual(self.store.list_run_events("run-live-missing-scope"), [])

    def test_live_execution_rejects_mismatched_bound_scope(self):
        self._make_run(
            "run-live-stale-scope",
            status="approved",
            scope_fingerprint="not-the-canonical-scope",
        )
        before = self.store.get_run("run-live-stale-scope")
        with self.assertRaisesRegex(ValueError, "scope fingerprint mismatch"):
            execute_supervised(self.store, "run-live-stale-scope")
        after = self.store.get_run("run-live-stale-scope")
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["row_version"], before["row_version"])
        self.assertEqual(self.store.list_run_events("run-live-stale-scope"), [])

    def test_approval_rejects_missing_bound_scope_before_lease_or_write(self):
        self._make_run(
            "run-approval-missing-scope",
            status="awaiting_approval",
            scope_fingerprint=None,
        )
        before = self.store.get_run("run-approval-missing-scope")
        with self.assertRaisesRegex(ValueError, "missing governed scope_fingerprint"):
            record_run_approval(
                self.store,
                "run-approval-missing-scope",
                requirement_type="shan_task_approval",
                decision="approved",
            )
        after = self.store.get_run("run-approval-missing-scope")
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["row_version"], before["row_version"])
        self.assertEqual(self.store.list_run_events("run-approval-missing-scope"), [])
        self.assertEqual(
            self.store.list_run_approvals("run-approval-missing-scope"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
'''
tests = replace_once(tests, insert_before, new_tests, label="scope entrypoint tests")
test_path.write_text(tests, encoding="utf-8")


approval_test_path = ROOT / "tests" / "test_run_approval_atomic.py"
approval_tests = approval_test_path.read_text(encoding="utf-8")
approval_tests = replace_once(
    approval_tests,
    '''        # New approval for second requirement should not complete set with stale first
        result = record_run_approval(
            self.store,
            run["id"],
            requirement_type="security_review",
            decision="approved",
            actor="shan",
        )
        # Still awaiting because first approval binding no longer matches
        self.assertEqual(result["run"]["status"], "awaiting_approval")
''',
    '''        # Authority drift invalidates the run itself. A new approval must fail
        # before writing history, events, or a status transition.
        before = self.store.get_run(run["id"])
        history_before = self.store.list_run_approval_history(run["id"])
        events_before = self.store.list_run_events(run["id"])
        with self.assertRaisesRegex(ValueError, "scope fingerprint mismatch"):
            record_run_approval(
                self.store,
                run["id"],
                requirement_type="security_review",
                decision="approved",
                actor="shan",
            )
        after = self.store.get_run(run["id"])
        self.assertEqual(after["status"], "awaiting_approval")
        self.assertEqual(after["row_version"], before["row_version"])
        self.assertEqual(
            self.store.list_run_approval_history(run["id"]),
            history_before,
        )
        self.assertEqual(self.store.list_run_events(run["id"]), events_before)
''',
    label="stale approval fail-closed expectation",
)
approval_test_path.write_text(approval_tests, encoding="utf-8")

print("Packet 5A scope entrypoint guards applied")
