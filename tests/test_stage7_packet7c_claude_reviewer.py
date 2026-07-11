"""Packet 7C contracts for a verified Claude read-only reviewer provider."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from buildforme.provider_compatibility import (
    clear_compat_cache,
    verify_provider_compatibility,
)
from buildforme.provider_discovery import probe_authentication
from buildforme.review_execution import (
    CLAUDE_REVIEW_SCHEMA_JSON,
    build_review_command,
    parse_strict_review_output,
)


class Packet7CClaudeReviewerTests(unittest.TestCase):
    def setUp(self):
        clear_compat_cache()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _fake_cli(self, *, logged_in=True):
        path = Path(self.temp.name) / "claude"
        help_text = " ".join(
            (
                "--print",
                "--output-format",
                "--json-schema",
                "--permission-mode",
                "--tools",
                "--strict-mcp-config",
                "--safe-mode",
                "--no-session-persistence",
            )
        )
        auth = {
            "loggedIn": bool(logged_in),
            "authMethod": "claude.ai" if logged_in else "none",
            "apiProvider": "firstParty",
        }
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"help_text = {help_text!r}\n"
            f"auth = {auth!r}\n"
            "args = sys.argv[1:]\n"
            "if args == ['auth', 'status']:\n"
            "    print(json.dumps(auth))\n"
            "    raise SystemExit(0 if auth['loggedIn'] else 1)\n"
            "if '--help' in args:\n"
            "    print(help_text)\n"
            "    raise SystemExit(0)\n"
            "if '--version' in args:\n"
            "    print('2.1.205 (Claude Code)')\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(2)\n",
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    @unittest.skipIf(os.name == "nt", "POSIX executable fixture")
    def test_claude_auth_status_json_is_machine_verified(self):
        executable = self._fake_cli(logged_in=True)
        result = probe_authentication("claude", executable)
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["probe_verified"])
        self.assertEqual(result["command_shape"], ["claude", "auth", "status"])

    @unittest.skipIf(os.name == "nt", "POSIX executable fixture")
    def test_claude_logged_out_json_fails_closed(self):
        executable = self._fake_cli(logged_in=False)
        result = probe_authentication("claude", executable)
        self.assertNotEqual(result["status"], "ready")
        self.assertFalse(result["probe_verified"])
        self.assertTrue(result["negative_status_marker"])

    @unittest.skipIf(os.name == "nt", "POSIX executable fixture")
    def test_claude_minimum_version_and_help_contract(self):
        executable = self._fake_cli(logged_in=True)
        auth = probe_authentication("claude", executable)
        current = verify_provider_compatibility(
            "claude",
            executable,
            version_text="2.1.205 (Claude Code)",
            auth_result=auth,
            force=True,
        )
        self.assertTrue(current["version_verified"])
        self.assertTrue(current["command_contract_verified"])
        self.assertTrue(current["auth_verified"])
        old = verify_provider_compatibility(
            "claude",
            executable,
            version_text="2.1.204 (Claude Code)",
            auth_result=auth,
            force=True,
        )
        self.assertFalse(old["version_verified"])
        self.assertTrue(any("below required" in item for item in old["problems"]))

    def test_claude_command_is_explicitly_read_only_and_structured(self):
        command = build_review_command("claude", "/tmp/claude")
        argv = command["argv"]
        self.assertEqual(command["contract_id"], "claude.print.plan.structured.v1")
        self.assertIn("--permission-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertIn("--safe-mode", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertEqual(argv[argv.index("--json-schema") + 1], CLAUDE_REVIEW_SCHEMA_JSON)
        self.assertNotIn("--dangerously-skip-permissions", argv)

    def test_claude_parser_requires_successful_structured_output(self):
        payload = {"verdict": "pass", "summary": "clear", "findings": []}
        wrapper = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": payload,
        }
        self.assertEqual(
            parse_strict_review_output("claude", json.dumps(wrapper)), payload
        )
        with self.assertRaisesRegex(ValueError, "result message"):
            parse_strict_review_output("claude", json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "did not complete successfully"):
            parse_strict_review_output(
                "claude",
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "error_max_structured_output_retries",
                        "is_error": True,
                    }
                ),
            )
        with self.assertRaisesRegex(ValueError, "missing structured_output"):
            parse_strict_review_output(
                "claude",
                json.dumps(
                    {"type": "result", "subtype": "success", "is_error": False}
                ),
            )


if __name__ == "__main__":
    unittest.main()
