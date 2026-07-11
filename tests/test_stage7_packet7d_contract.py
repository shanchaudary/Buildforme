from __future__ import annotations

import ast
import unittest
from pathlib import Path


class Stage7Packet7DContractTests(unittest.TestCase):
    def test_repair_service_has_no_unrestricted_run_write(self):
        source = Path("buildforme/repair_service.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"save_run", "save_run_for_setup", "save_run_legacy_json"}:
                    forbidden.append((node.func.attr, node.lineno))
        self.assertEqual(forbidden, [])

    def test_storage_owns_repair_packet_validation(self):
        source = Path("buildforme/execution_store.py").read_text(encoding="utf-8")
        self.assertIn("validate_repair_packet_for_storage", source)
        self.assertIn("source cycle may create only one", source)
        self.assertIn("stage7_repair_packet_id", source)


if __name__ == "__main__":
    unittest.main()
