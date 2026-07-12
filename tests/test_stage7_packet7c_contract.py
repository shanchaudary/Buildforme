"""Permanent source contracts for Packet 7C distinct-provider review."""

from __future__ import annotations

import unittest
from pathlib import Path


class Packet7CContractTests(unittest.TestCase):
    def test_claude_contract_remains_read_only_and_machine_verified(self):
        source = Path("buildforme/review_execution.py").read_text(encoding="utf-8")
        for phrase in (
            '"claude"',
            '"--permission-mode"',
            '"plan"',
            '"Read,Grep,Glob"',
            '"--json-schema"',
            '"--safe-mode"',
            '"--strict-mcp-config"',
            '"--no-session-persistence"',
            '"structured_output"',
        ):
            self.assertIn(phrase, source)
        discovery = Path("buildforme/provider_discovery.py").read_text(encoding="utf-8")
        self.assertIn('"args": ["auth", "status"]', discovery)
        compatibility = Path("buildforme/provider_compatibility.py").read_text(encoding="utf-8")
        self.assertIn('"min_version": (2, 1, 205)', compatibility)


if __name__ == "__main__":
    unittest.main()
