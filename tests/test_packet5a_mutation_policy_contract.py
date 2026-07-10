"""Permanent contract tests for Packet 5A mutation authority."""

from __future__ import annotations

import unittest

from buildforme.execution_store import (
    MUTATION_METADATA_ALLOWLISTS,
    PROTECTED_AUTHORITY_FIELDS,
    _MUTATION_ALLOWED_EDGES,
    _MUTATION_ALLOWED_EXECUTION_MODES,
    _MUTATION_ALLOWED_STATUSES,
    _MUTATION_ALLOW_SAME_STATE,
)


class Packet5AMutationPolicyContractTests(unittest.TestCase):
    def test_policy_tables_cover_exactly_the_same_mutation_types(self):
        mutation_types = set(MUTATION_METADATA_ALLOWLISTS)
        self.assertEqual(set(_MUTATION_ALLOWED_STATUSES), mutation_types)
        self.assertEqual(set(_MUTATION_ALLOWED_EDGES), mutation_types)
        self.assertEqual(set(_MUTATION_ALLOWED_EXECUTION_MODES), mutation_types)
        self.assertLessEqual(set(_MUTATION_ALLOW_SAME_STATE), mutation_types)

    def test_no_mutation_allowlist_contains_protected_authority(self):
        for mutation_type, allowed_fields in MUTATION_METADATA_ALLOWLISTS.items():
            overlap = set(allowed_fields) & set(PROTECTED_AUTHORITY_FIELDS)
            self.assertEqual(
                overlap,
                set(),
                msg=f"{mutation_type} may rewrite protected authority: {sorted(overlap)}",
            )

    def test_metadata_only_mutations_have_no_state_edges(self):
        for mutation_type in (
            "process_result",
            "verification_result",
            "execution_evidence_link",
            "review_package",
            "constitution_compliance",
            "worktree_prepared",
        ):
            self.assertEqual(
                _MUTATION_ALLOWED_EDGES[mutation_type],
                frozenset(),
                msg=f"{mutation_type} must remain metadata-only",
            )
            self.assertIn(mutation_type, _MUTATION_ALLOW_SAME_STATE)

    def test_generic_status_transition_has_one_narrow_edge(self):
        self.assertEqual(
            _MUTATION_ALLOWED_EDGES["status_transition"],
            frozenset({("draft", "awaiting_preflight")}),
        )
        self.assertNotIn("status_transition", _MUTATION_ALLOW_SAME_STATE)

    def test_completion_mutations_are_bound_to_execution_mode(self):
        self.assertEqual(
            _MUTATION_ALLOWED_EXECUTION_MODES["dry_run_finished"],
            frozenset({"dry_run"}),
        )
        self.assertEqual(
            _MUTATION_ALLOWED_EXECUTION_MODES["supervised_finished"],
            frozenset({"live_supervised"}),
        )

    def test_completion_requires_designated_mutation_classes(self):
        completion_edges = {
            edge
            for edges in _MUTATION_ALLOWED_EDGES.values()
            for edge in edges
            if edge[1] == "completed"
        }
        self.assertEqual(completion_edges, {("needs_review", "completed")})
        self.assertIn(
            ("needs_review", "completed"),
            _MUTATION_ALLOWED_EDGES["dry_run_finished"],
        )


if __name__ == "__main__":
    unittest.main()
