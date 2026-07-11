from __future__ import annotations

import ast
import unittest
from pathlib import Path


class Stage7Packet7DAdmissionContractTests(unittest.TestCase):
    def test_repair_seed_does_not_run_shell_or_push(self):
        source = Path("buildforme/repair_seed.py").read_text(encoding="utf-8")
        self.assertIn("shell=False", source)
        self.assertNotIn('"push"', source)
        self.assertIn("refs/buildforme/repair-seeds/", source)

    def test_repair_service_uses_only_dedicated_atomic_admission(self):
        source = Path("buildforme/repair_service.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        self.assertIn("admit_repair_run_atomic", calls)
        self.assertNotIn("save_run", calls)
        self.assertNotIn("save_run_for_setup", calls)

    def test_scope_and_protected_authority_include_seed(self):
        governance = Path("buildforme/governance.py").read_text(encoding="utf-8")
        storage = Path("buildforme/execution_store.py").read_text(encoding="utf-8")
        for token in ("execution_seed_commit", "repair_packet_id", "repair_fingerprint"):
            self.assertIn(token, governance)
            self.assertIn(token, storage)


if __name__ == "__main__":
    unittest.main()
