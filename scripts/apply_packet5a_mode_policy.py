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

old = '''_MUTATION_ALLOW_SAME_STATE: frozenset[str] = frozenset(
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
new = '''_MUTATION_ALLOW_SAME_STATE: frozenset[str] = frozenset(
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


def _values_equal'''
text = replace_once(text, old, new, label="execution mode policy insertion")

old = '''        allow = MUTATION_METADATA_ALLOWLISTS[mutation_type]
        allowed_statuses = _MUTATION_ALLOWED_STATUSES[mutation_type]

        with self.db.transaction() as conn:
'''
new = '''        allow = MUTATION_METADATA_ALLOWLISTS[mutation_type]
        allowed_statuses = _MUTATION_ALLOWED_STATUSES[mutation_type]
        allowed_execution_modes = _MUTATION_ALLOWED_EXECUTION_MODES[mutation_type]

        with self.db.transaction() as conn:
'''
text = replace_once(text, old, new, label="execution mode policy binding")

old = '''            db_status = str(existing[3] or db_payload.get("status") or "")
            new_status = str(proposed.get("status") or db_status)

            if db_status not in allowed_statuses:
'''
new = '''            db_status = str(existing[3] or db_payload.get("status") or "")
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
'''
text = replace_once(text, old, new, label="execution mode enforcement")

old = '''            elif mutation_type not in _MUTATION_ALLOW_SAME_STATE:
                raise ValueError(
                    f"mutation_type {mutation_type!r} requires an authorized status transition"
                )

            # Apply a change set to canonical storage state; never replace the
'''
new = '''            elif mutation_type not in _MUTATION_ALLOW_SAME_STATE:
                raise ValueError(
                    f"mutation_type {mutation_type!r} requires an authorized status transition"
                )

            if mutation_type == "preflight_result" and "approval_requirements" in changed:
                requirement_edges = {
                    ("awaiting_preflight", "awaiting_approval"),
                    ("awaiting_preflight", "approved"),
                    ("awaiting_approval", "approved"),
                }
                if len(edges) != 1 or edges[0] not in requirement_edges:
                    raise ValueError(
                        "approval_requirements may change only on an authorized "
                        "preflight admission edge"
                    )

            # Apply a change set to canonical storage state; never replace the
'''
text = replace_once(text, old, new, label="approval requirement policy")
store_path.write_text(text, encoding="utf-8")


contract_path = ROOT / "tests" / "test_packet5a_mutation_policy_contract.py"
contract = contract_path.read_text(encoding="utf-8")
contract = replace_once(
    contract,
    '''    _MUTATION_ALLOWED_EDGES,
    _MUTATION_ALLOW_SAME_STATE,
)''',
    '''    _MUTATION_ALLOWED_EDGES,
    _MUTATION_ALLOWED_EXECUTION_MODES,
    _MUTATION_ALLOW_SAME_STATE,
)''',
    label="contract import",
)
contract = replace_once(
    contract,
    '''    def test_completion_requires_designated_mutation_classes(self):
''',
    '''    def test_completion_mutations_are_bound_to_execution_mode(self):
        self.assertEqual(
            _MUTATION_ALLOWED_EXECUTION_MODES["dry_run_finished"],
            frozenset({"dry_run"}),
        )
        self.assertEqual(
            _MUTATION_ALLOWED_EXECUTION_MODES["supervised_finished"],
            frozenset({"live_supervised"}),
        )

    def test_completion_requires_designated_mutation_classes(self):
''',
    label="contract execution mode test",
)
contract_path.write_text(contract, encoding="utf-8")


hardening_path = ROOT / "tests" / "test_run_mutation_authority_hardening.py"
tests = hardening_path.read_text(encoding="utf-8")
insert_before = '''

if __name__ == "__main__":
    unittest.main()
'''
new_tests = '''

    def test_live_run_cannot_complete_through_dry_run_mutation(self):
        self._make_run("run-live-dry-bypass", status="running")
        run = self.store.get_run("run-live-dry-bypass")
        version = run["row_version"]
        run["status"] = "completed"
        run["dry_run_result"] = {"ok": True}
        with self.assertRaisesRegex(ValueError, "not permitted for execution_mode"):
            self._commit(
                run,
                mutation_type="dry_run_finished",
                transition_path=["running", "needs_review", "completed"],
            )
        stored = self.store.get_run("run-live-dry-bypass")
        self.assertEqual(stored["status"], "running")
        self.assertEqual(stored["row_version"], version)
        self.assertEqual(self.store.list_run_events("run-live-dry-bypass"), [])

    def test_dry_run_cannot_use_supervised_completion_mutation(self):
        self._make_run(
            "run-dry-live-bypass",
            status="running",
            execution_mode="dry_run",
            mode="dry_run",
            transport="dry_run",
        )
        run = self.store.get_run("run-dry-live-bypass")
        version = run["row_version"]
        run["status"] = "needs_review"
        run["review"] = {"status": "review_required"}
        with self.assertRaisesRegex(ValueError, "not permitted for execution_mode"):
            self._commit(run, mutation_type="supervised_finished")
        stored = self.store.get_run("run-dry-live-bypass")
        self.assertEqual(stored["status"], "running")
        self.assertEqual(stored["row_version"], version)
        self.assertEqual(self.store.list_run_events("run-dry-live-bypass"), [])

    def test_same_state_preflight_cannot_rewrite_approval_requirements(self):
        self._make_run(
            "run-approved-requirements",
            status="approved",
            approval_requirements=["shan_task_approval"],
        )
        run = self.store.get_run("run-approved-requirements")
        version = run["row_version"]
        run["approval_requirements"] = []
        run["preflight"] = {"passed": True, "required_approvals": []}
        with self.assertRaisesRegex(ValueError, "authorized preflight admission edge"):
            self._commit(run, mutation_type="preflight_result")
        stored = self.store.get_run("run-approved-requirements")
        self.assertEqual(stored["approval_requirements"], ["shan_task_approval"])
        self.assertEqual(stored["row_version"], version)
        self.assertEqual(self.store.list_run_events("run-approved-requirements"), [])

    def test_initial_preflight_edge_may_set_approval_requirements(self):
        self._make_run(
            "run-initial-requirements",
            status="awaiting_preflight",
            approval_requirements=[],
        )
        run = self.store.get_run("run-initial-requirements")
        run["status"] = "awaiting_approval"
        run["approval_requirements"] = ["shan_task_approval"]
        run["preflight"] = {
            "passed": True,
            "required_approvals": ["shan_task_approval"],
        }
        saved = self._commit(
            run,
            mutation_type="preflight_result",
            event_type="preflight_passed",
        )
        self.assertEqual(saved["status"], "awaiting_approval")
        self.assertEqual(saved["approval_requirements"], ["shan_task_approval"])


if __name__ == "__main__":
    unittest.main()
'''
tests = replace_once(tests, insert_before, new_tests, label="mode and approval tests")
hardening_path.write_text(tests, encoding="utf-8")

print("Packet 5A execution-mode and approval policy applied")
