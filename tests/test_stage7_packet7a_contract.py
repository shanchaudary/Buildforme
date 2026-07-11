"""Permanent source contracts for accepted Stage 7 Packet 7A authority."""

from __future__ import annotations

import unittest
from pathlib import Path

from buildforme.db import SCHEMA_VERSION


class Stage7Packet7AContractTests(unittest.TestCase):
    def test_schema_and_review_shopping_authority_are_permanent(self):
        self.assertEqual(SCHEMA_VERSION, 4)
        source = Path("buildforme/execution_store.py").read_text(encoding="utf-8")
        self.assertIn(
            'SELECT id, status FROM review_cycles WHERE run_id=? AND evidence_id=?',
            source,
        )
        self.assertIn("a new cycle requires fresh repair and execution evidence", source)

    def test_blind_api_uses_withholding_service(self):
        service = Path("buildforme/review_service.py").read_text(encoding="utf-8")
        server = Path("buildforme/server.py").read_text(encoding="utf-8")
        self.assertIn("def get_independent_review_cycle_view", service)
        self.assertIn('"reports": store.list_review_reports(cycle_id) if finalized else []', service)
        self.assertIn("get_independent_review_cycle_view(self._store(), cycle_id)", server)

    def test_non_weakenable_policy_and_finding_validation_are_present(self):
        source = Path("buildforme/review_contracts.py").read_text(encoding="utf-8")
        for phrase in (
            "review policy cannot weaken",
            "def validate_finding_for_storage",
            "critical/high review finding must be blocking",
            "review finding fingerprint mismatch",
        ):
            self.assertIn(phrase, source)


if __name__ == "__main__":
    unittest.main()
