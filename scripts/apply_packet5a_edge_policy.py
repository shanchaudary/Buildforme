from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


store_path = ROOT / "buildforme" / "execution_store.py"
text = store_path.read_text(encoding="utf-8")

marker = "}\n\ndef _values_equal"
policy = '''}

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


def _values_equal'''
text = replace_once(text, marker, policy, label="edge policy insertion")

old = '''            elif transition_path is not None:
                raise ValueError("transition_path is forbidden for a same-state mutation")

            # Apply a change set to canonical storage state; never replace the
'''
new = '''            elif transition_path is not None:
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

            # Apply a change set to canonical storage state; never replace the
'''
text = replace_once(text, old, new, label="edge policy enforcement")
store_path.write_text(text, encoding="utf-8")


test_path = ROOT / "tests" / "test_run_mutation_authority_hardening.py"
tests = test_path.read_text(encoding="utf-8")
insert_before = '''

if __name__ == "__main__":
    unittest.main()
'''
new_tests = '''

    def test_metadata_mutation_types_cannot_claim_completion_edges(self):
        cases = {
            "process_result": ("process_result", {"ok": True}),
            "verification_result": ("verification", {"passed": True}),
            "execution_evidence_link": ("evidence", {"evidence_id": "ev"}),
            "review_package": ("review", {"status": "review_required"}),
        }
        for index, (mutation_type, (field, value)) in enumerate(cases.items()):
            run_id = f"run-edge-policy-{index}"
            self._make_run(run_id, status="running")
            run = self.store.get_run(run_id)
            version = run["row_version"]
            run[field] = value
            run["status"] = "completed"
            with self.assertRaisesRegex(ValueError, "does not authorize transition edge"):
                self._commit(run, mutation_type=mutation_type)
            stored = self.store.get_run(run_id)
            self.assertEqual(stored["status"], "running")
            self.assertEqual(stored["row_version"], version)
            self.assertEqual(self.store.list_run_events(run_id), [])

    def test_generic_status_transition_cannot_bypass_review(self):
        self._make_run("run-generic-edge", status="running")
        run = self.store.get_run("run-generic-edge")
        version = run["row_version"]
        run["status"] = "completed"
        with self.assertRaisesRegex(ValueError, "does not authorize transition edge"):
            self._commit(run, mutation_type="status_transition")
        stored = self.store.get_run("run-generic-edge")
        self.assertEqual(stored["status"], "running")
        self.assertEqual(stored["row_version"], version)
        self.assertEqual(self.store.list_run_events("run-generic-edge"), [])

    def test_transition_only_mutation_cannot_be_used_same_state(self):
        self._make_run("run-transition-same", status="draft", started_at=None)
        run = self.store.get_run("run-transition-same")
        with self.assertRaisesRegex(ValueError, "requires an authorized status transition"):
            self._commit(run, mutation_type="status_transition")

    def test_failure_and_cancel_edges_remain_authorized(self):
        self._make_run("run-failure-edge", status="running")
        failed = self.store.get_run("run-failure-edge")
        failed["status"] = "failed"
        failed["process_result"] = {"ok": False, "error": "provider failed"}
        failed = self._commit(
            failed,
            mutation_type="failure_detail",
            event_type="supervised_failed",
        )
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["finished_at"])

        self._make_run("run-cancel-edge", status="running")
        cancelled = self.store.get_run("run-cancel-edge")
        cancelled["status"] = "cancelled"
        cancelled["process_result"] = {"cancelled": True}
        cancelled = self._commit(
            cancelled,
            mutation_type="cancel",
            event_type="supervised_cancelled",
            transition_path=["running", "cancel_requested", "cancelled"],
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(len(cancelled["status_history"]), 2)


if __name__ == "__main__":
    unittest.main()
'''
tests = replace_once(tests, insert_before, new_tests, label="edge policy tests")
test_path.write_text(tests, encoding="utf-8")

print("Packet 5A mutation-edge policy applied")
