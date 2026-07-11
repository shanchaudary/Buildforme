from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


path = ROOT / "buildforme" / "execution_store.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''            if conn.execute(
                "SELECT id FROM review_cycles WHERE run_id=? AND status IN ('open','collecting','ready_to_aggregate')",
                (run_id,),
            ).fetchone():
                raise ValueError("an active independent review cycle already exists for this run")
''',
    '''            prior_same_evidence = conn.execute(
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
''',
    label="review shopping block",
)
path.write_text(text, encoding="utf-8")

path = ROOT / "buildforme" / "execution_service.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''    run = store.get_run(run_id)
    actor = validate_actor(actor)
    current = str(run.get("status") or "")
    if decision == "accept_for_pr_prep" and run.get("stage7_review_required"):
''',
    '''    run = store.get_run(run_id)
    actor = validate_actor(actor)
    decision = str(decision or "").strip().lower()
    current = str(run.get("status") or "")
    if decision == "accept_for_pr_prep" and run.get("stage7_review_required"):
''',
    label="founder decision normalization",
)
path.write_text(text, encoding="utf-8")

path = ROOT / "tests" / "test_stage7_review_authority.py"
text = path.read_text(encoding="utf-8")
insert_before = '''    def test_review_service_has_no_unrestricted_run_write(self):
'''
new_tests = r'''    def test_blocking_cycle_cannot_be_re_reviewed_without_fresh_evidence(self):
        result = self._cycle()
        first, second = result["assignments"]
        submit_independent_review_report(
            self.store,
            first["cycle_id"],
            first["assignment_id"],
            payload={
                "verdict": "changes_required",
                "summary": "repair required",
                "findings": [
                    {
                        "severity": "high",
                        "category": "governance",
                        "summary": "authority defect",
                        "evidence": "exact failing path",
                        "recommendation": "repair and re-execute",
                    }
                ],
            },
        )
        self._pass_report(second)
        finalized = aggregate_independent_review_cycle(
            self.store, result["cycle"]["cycle_id"]
        )
        self.assertEqual(finalized["cycle"]["status"], "repair_required")
        with self.assertRaisesRegex(ValueError, "fresh repair and execution evidence"):
            create_independent_review_cycle(
                self.store,
                self.run["id"],
                reviewers=self.reviewers,
                actor="shan",
            )

    def test_founder_decision_is_normalized_before_stage7_gate(self):
        source = Path("buildforme/execution_service.py").read_text(encoding="utf-8")
        normalization = 'decision = str(decision or "").strip().lower()'
        gate = 'if decision == "accept_for_pr_prep" and run.get("stage7_review_required")'
        self.assertIn(normalization, source)
        self.assertIn(gate, source)
        self.assertLess(source.index(normalization), source.index(gate))

'''
text = replace_once(text, insert_before, new_tests + insert_before, label="review shopping tests")
path.write_text(text, encoding="utf-8")

path = ROOT / "docs" / "STAGE_7_INDEPENDENT_MULTI_AGENT_REVIEW.md"
text = path.read_text(encoding="utf-8")
text += '''
- Review shopping is prohibited: an execution-evidence record can be bound to only
  one independent review cycle. A repair verdict requires fresh repair execution
  evidence and re-verification before another cycle can begin.
'''
path.write_text(text, encoding="utf-8")

print("Stage 7 Packet 7A review-shopping remediation applied")
