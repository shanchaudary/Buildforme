from __future__ import annotations

import unittest
from pathlib import Path


class Stage7Packet7ERedTeamContracts(unittest.TestCase):
    def test_repair_api_actor_comes_only_from_founder_session(self):
        server = Path("buildforme/server.py").read_text(encoding="utf-8")
        repair_handler = server.split("def _stage7_repair_action", 1)[1].split(
            "def _stage7_review_action", 1
        )[0]
        self.assertIn('actor = str(auth.get("actor") or "shan")', repair_handler)
        self.assertNotIn('payload.get("actor")', repair_handler)

    def test_smoke_no_merge_and_report_truth_are_derived(self):
        evaluator = Path("buildforme/stage7_smoke.py").read_text(encoding="utf-8")
        script = Path("scripts/stage7_real_two_provider_smoke.py").read_text(encoding="utf-8")
        self.assertIn("execution_reports_match_storage_and_aggregate", evaluator)
        self.assertIn("cycle_bound_to_exact_evidence", evaluator)
        self.assertIn("merge_commit_count", evaluator)
        self.assertIn('"rev-list", "--count", "--merges"', script)
        self.assertNotIn('"merge_performed": False', script)
        self.assertNotIn('"direct_report_submission_used": False', script)


if __name__ == "__main__":
    unittest.main()
