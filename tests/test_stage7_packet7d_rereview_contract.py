from __future__ import annotations

import unittest
from pathlib import Path


class Stage7Packet7DRereviewContractTests(unittest.TestCase):
    def test_repair_child_founder_gate_is_permanent(self):
        source = Path("buildforme/repair_service.py").read_text(encoding="utf-8")
        self.assertIn('"stage7_review_required": True', source)
        self.assertIn("create_repair_review_cycle", source)
        self.assertIn("execute_governed_repair_and_open_review", source)

    def test_single_review_cycle_authority_owns_repair_link(self):
        source = Path("buildforme/execution_store.py").read_text(encoding="utf-8")
        self.assertIn("repair_review_links", source)
        self.assertIn("exactly reuse source reviewer providers", source)
        self.assertIn("repair implementer cannot participate", source)


if __name__ == "__main__":
    unittest.main()
