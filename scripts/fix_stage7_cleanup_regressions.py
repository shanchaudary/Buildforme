from __future__ import annotations

from pathlib import Path

path = Path("tests/test_stage7_review_authority.py")
text = path.read_text(encoding="utf-8")

replacements = [
    (
        '''        self.assertIn("Stage 7 independent review requires repair", blocks)\n''',
        '''        self.assertIn("Stage 7 independent review is not clear", blocks)\n        self.assertIn("Stage 7 independent review contains blocking findings", blocks)\n''',
        "blocking review expectation",
    ),
    (
        '''        with self.assertRaisesRegex(ValueError, "fixture review assignment is not pending"):\n''',
        '''        with self.assertRaisesRegex(\n            ValueError, "not pending or executing|fixture review assignment is not pending"\n        ):\n''',
        "append-only expectation",
    ),
    (
        '''        with self.assertRaisesRegex(ValueError, "assignment set"):\n''',
        '''        with self.assertRaisesRegex(ValueError, "exactly match"):\n''',
        "assignment set expectation",
    ),
]
for old, new, label in replacements:
    if text.count(old) != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {text.count(old)}")
    text = text.replace(old, new, 1)

old_policy = '''    def test_policy_cannot_disable_blind_or_blocking_laws(self):
        result = create_independent_review_cycle(
            self.store,
            self.run["id"],
            reviewers=self.reviewers,
            actor="shan",
            policy={
                "blind_review": False,
                "implementer_provider_forbidden": False,
                "critical_high_always_blocking": False,
                "founder_override_blocking_findings": True,
            },
        )
        policy = result["cycle"]["policy"]
        self.assertTrue(policy["blind_review"])
        self.assertTrue(policy["implementer_provider_forbidden"])
        self.assertTrue(policy["critical_high_always_blocking"])
        self.assertFalse(policy["founder_override_blocking_findings"])
'''
new_policy = '''    def test_policy_cannot_disable_blind_or_blocking_laws(self):
        for policy in (
            {"blind_review": False},
            {"implementer_provider_forbidden": False},
            {"critical_high_always_blocking": False},
            {"founder_override_blocking_findings": True},
        ):
            with self.subTest(policy=policy):
                with self.assertRaisesRegex(ValueError, "cannot weaken"):
                    create_independent_review_cycle(
                        self.store,
                        self.run["id"],
                        reviewers=self.reviewers,
                        actor="shan",
                        policy=policy,
                    )
'''
if text.count(old_policy) != 1:
    raise RuntimeError(f"policy expectation: expected one block, found {text.count(old_policy)}")
text = text.replace(old_policy, new_policy, 1)

old_blind = '''    def test_blind_cycle_view_withholds_reports_until_finalized(self):
        result = self._cycle()
        self._pass_report(result["assignments"][0])
        view = get_independent_review_cycle_view(self.store, result["cycle"]["cycle_id"])
        self.assertTrue(view["blind_withheld"])
        self.assertNotIn("reports", view)
        self.assertNotIn("findings", view)
        self.assertEqual(view["submitted_reviewer_count"], 1)
'''
new_blind = '''    def test_blind_cycle_view_withholds_reports_until_finalized(self):
        result = self._cycle()
        self._pass_report(result["assignments"][0])
        active = get_independent_review_cycle_view(
            self.store, result["cycle"]["cycle_id"]
        )
        self.assertTrue(active["blind_material_withheld"])
        self.assertEqual(active["reports"], [])
        self.assertEqual(active["findings"], [])
        self._pass_report(result["assignments"][1])
        aggregate_independent_review_cycle(
            self.store, result["cycle"]["cycle_id"]
        )
        final = get_independent_review_cycle_view(
            self.store, result["cycle"]["cycle_id"]
        )
        self.assertFalse(final["blind_material_withheld"])
        self.assertEqual(len(final["reports"]), 2)
'''
if text.count(old_blind) != 1:
    raise RuntimeError(f"blind-view expectation: expected one block, found {text.count(old_blind)}")
text = text.replace(old_blind, new_blind, 1)

path.write_text(text, encoding="utf-8")
print("Stage 7 cleanup regression expectations corrected")
