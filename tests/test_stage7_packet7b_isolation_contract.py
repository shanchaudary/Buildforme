from __future__ import annotations

import ast
import unittest
from pathlib import Path


class Packet7BIsolationContractTests(unittest.TestCase):
    def test_reviewer_never_runs_in_authoritative_worktree(self):
        source = Path("buildforme/review_execution.py").read_text(encoding="utf-8")
        self.assertIn("_create_isolated_review_workspace", source)
        self.assertIn("cwd=review_root", source)
        self.assertNotIn("cwd=authoritative_root", source)
        self.assertIn("workspace_tree_fingerprint", source)
        self.assertIn("workspace_holder.cleanup()", source)

    def test_packet_carries_bound_constitution_reminder(self):
        source = Path("buildforme/review_execution.py").read_text(encoding="utf-8")
        self.assertIn('"constitution_reminder": get_engine().reminder(', source)
        self.assertIn('phase="independent_review"', source)
        self.assertIn('"constitution_reminder",', source)
        self.assertIn('engine.content_hash()', source)
        self.assertIn('canonical Constitution text does not match', source)

    def test_review_executor_has_no_unrestricted_run_write(self):
        tree = ast.parse(Path("buildforme/review_execution.py").read_text(encoding="utf-8"))
        forbidden = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"save_run", "save_run_for_setup", "save_run_legacy_json"}:
                    forbidden.append((node.func.attr, node.lineno))
        self.assertEqual(forbidden, [])


if __name__ == "__main__":
    unittest.main()
