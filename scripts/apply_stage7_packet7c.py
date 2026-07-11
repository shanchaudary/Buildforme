from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# Claude machine-verifiable authentication status.
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "provider_discovery.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''    "codex": {
        "args": ["login", "status"],
        "success_exit_codes": [0],
        "positive_patterns": [r"(?i)logged in"],
        "failure_patterns": [r"(?i)not logged in", r"(?i)logged out"],
        "contract_source": "openai/codex codex-rs/cli/src/login.rs run_login_status",
        "read_only": True,
    },
''',
    '''    "codex": {
        "args": ["login", "status"],
        "success_exit_codes": [0],
        "positive_patterns": [r"(?i)logged in"],
        "failure_patterns": [r"(?i)not logged in", r"(?i)logged out"],
        "contract_source": "openai/codex codex-rs/cli/src/login.rs run_login_status",
        "read_only": True,
    },
    "claude": {
        "args": ["auth", "status"],
        "success_exit_codes": [0],
        "positive_patterns": [r'"loggedIn"\\s*:\\s*true'],
        "failure_patterns": [r'"loggedIn"\\s*:\\s*false'],
        "contract_source": "Claude Code CLI reference: claude auth status JSON",
        "read_only": True,
    },
''',
    label="Claude auth probe contract",
)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Claude compatibility: verified flags and minimum safe structured-output version.
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "provider_compatibility.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''    "claude": {
        "min_major": 1,
        "required_help_tokens": ["--print", "-p", "--output-format"],
        "help_argv": ["--help"],
        "version_argv": ["--version"],
        "non_interactive_tokens": ["--print", "non-interactive"],
        "prompt_delivery": "arg",
        "cwd_flag": None,
    },
''',
    '''    "claude": {
        "min_major": 2,
        "min_version": (2, 1, 205),
        "required_help_tokens": [
            "--print",
            "--output-format",
            "--json-schema",
            "--permission-mode",
            "--tools",
            "--strict-mcp-config",
            "--safe-mode",
            "--no-session-persistence",
        ],
        "help_argv": ["--help"],
        "version_argv": ["--version"],
        "non_interactive_tokens": ["--print"],
        "prompt_delivery": "arg",
        "cwd_flag": None,
    },
''',
    label="Claude compatibility profile",
)
text = replace_once(
    text,
    '''    else:
        result["version_verified"] = True

    # Help / contract probe
''',
    '''    else:
        result["version_verified"] = True

    minimum = profile.get("min_version")
    if minimum:
        parsed_version = parse_version_tuple(version_text)
        result["minimum_version"] = ".".join(str(part) for part in minimum)
        result["parsed_version"] = (
            ".".join(str(part) for part in parsed_version)
            if parsed_version is not None
            else None
        )
        if parsed_version is None:
            result["version_verified"] = False
            result["problems"].append("provider version could not be parsed")
        elif parsed_version < tuple(int(part) for part in minimum):
            result["version_verified"] = False
            result["problems"].append(
                f"provider version {result['parsed_version']} is below required {result['minimum_version']}"
            )

    # Help / contract probe
''',
    label="minimum version validation",
)
text = replace_once(
    text,
    '''def parse_major_version(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"(\\d+)\\.\\d+", text)
    if not m:
        return None
    return int(m.group(1))
''',
    '''def parse_version_tuple(text: str | None) -> tuple[int, int, int] | None:
    if not text:
        return None
    match = re.search(r"(\\d+)\\.(\\d+)\\.(\\d+)", text)
    if not match:
        return None
    return tuple(int(match.group(index)) for index in (1, 2, 3))


def parse_major_version(text: str | None) -> int | None:
    parsed = parse_version_tuple(text)
    if parsed is not None:
        return parsed[0]
    if not text:
        return None
    match = re.search(r"(\\d+)\\.\\d+", text)
    if not match:
        return None
    return int(match.group(1))
''',
    label="semantic version parser",
)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Claude read-only reviewer command and strict structured-output parser.
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "review_execution.py"
text = path.read_text(encoding="utf-8")
contract_anchor = '''# Reviewed code authority only. Provider records and API payloads cannot alter argv.
REVIEW_COMMAND_CONTRACTS: dict[str, dict[str, Any]] = {
'''
schema_and_anchor = '''CLAUDE_REVIEW_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "changes_required", "block"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low", "info"],
                    },
                    "category": {"type": "string"},
                    "blocking": {"type": "boolean"},
                    "summary": {"type": "string"},
                    "evidence": {"type": "string"},
                    "recommendation": {"type": "string"},
                    "file": {"type": ["string", "null"]},
                    "line": {"type": ["integer", "null"]},
                    "law_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "severity",
                    "category",
                    "blocking",
                    "summary",
                    "evidence",
                    "recommendation",
                ],
            },
        },
    },
    "required": ["verdict", "summary", "findings"],
}
CLAUDE_REVIEW_SCHEMA_JSON = json.dumps(
    CLAUDE_REVIEW_JSON_SCHEMA,
    sort_keys=True,
    separators=(",", ":"),
)

# Reviewed code authority only. Provider records and API payloads cannot alter argv.
REVIEW_COMMAND_CONTRACTS: dict[str, dict[str, Any]] = {
'''
if text.count(contract_anchor) != 1:
    raise RuntimeError("review command contract anchor missing")
text = text.replace(contract_anchor, schema_and_anchor, 1)
text = replace_once(
    text,
    '''    "codex": {
        "contract_id": "codex.exec.read-only.v1",
        "argv_tail": [
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            "--json",
            "-s",
            "read-only",
            "-",
        ],
        "prompt_transport": "stdin",
        "output_protocol": "codex_jsonl_agent_message",
        "read_only": True,
    },
}
''',
    '''    "codex": {
        "contract_id": "codex.exec.read-only.v1",
        "argv_tail": [
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            "--json",
            "-s",
            "read-only",
            "-",
        ],
        "prompt_transport": "stdin",
        "output_protocol": "codex_jsonl_agent_message",
        "read_only": True,
    },
    "claude": {
        "contract_id": "claude.print.plan.structured.v1",
        "argv_tail": [
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            CLAUDE_REVIEW_SCHEMA_JSON,
            "--permission-mode",
            "plan",
            "--tools",
            "Read,Grep,Glob",
            "--strict-mcp-config",
            "--safe-mode",
            "--no-session-persistence",
            "Read the blind-review packet from stdin, inspect the repository read-only, and return the validated structured review.",
        ],
        "prompt_transport": "stdin_with_query",
        "output_protocol": "claude_json_structured_output",
        "read_only": True,
        "minimum_version": "2.1.205",
    },
}
''',
    label="Claude review command contract",
)
start = text.index("def parse_strict_review_output(")
end = text.index("\n\ndef build_review_execution_record(", start)
parser = '''def parse_strict_review_output(provider_id: str, stdout: str) -> dict[str, Any]:
    text = str(stdout or "")
    if not text.strip():
        raise ValueError("reviewer produced no structured output")
    provider = str(provider_id or "").strip().lower()
    candidates: list[Any] = []

    if provider == "claude":
        try:
            wrapper = json.loads(text.strip())
        except json.JSONDecodeError as exc:
            raise ValueError("Claude reviewer output is not one JSON result object") from exc
        if not isinstance(wrapper, dict):
            raise ValueError("Claude reviewer output wrapper must be an object")
        if str(wrapper.get("type") or "") != "result":
            raise ValueError("Claude reviewer output is not a result message")
        if str(wrapper.get("subtype") or "") != "success" or wrapper.get("is_error") is True:
            raise ValueError("Claude reviewer structured output did not complete successfully")
        structured = wrapper.get("structured_output")
        if not isinstance(structured, dict):
            raise ValueError("Claude reviewer result missing structured_output")
        candidates.append(structured)
    elif provider == "codex":
        direct = text.strip()
        try:
            parsed = json.loads(direct)
            if isinstance(parsed, dict) and "verdict" in parsed:
                candidates.append(parsed)
        except json.JSONDecodeError:
            pass
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if str(item.get("type") or "") != "agent_message":
                continue
            message = item.get("text") or item.get("content")
            if not isinstance(message, str):
                continue
            if message.strip().startswith("```"):
                raise ValueError("review output must not use markdown fences")
            try:
                candidate = json.loads(message.strip())
            except json.JSONDecodeError as exc:
                raise ValueError("reviewer agent_message is not strict JSON") from exc
            candidates.append(candidate)
    else:
        raise ValueError(f"unsupported review output protocol for provider {provider}")

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = _validate_payload_shape(candidate)
        key = json.dumps(candidate, sort_keys=True, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    if len(unique) != 1:
        raise ValueError(
            f"review output must contain exactly one unambiguous review object, found {len(unique)}"
        )
    return unique[0]
'''
text = text[:start] + parser + text[end:]
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Expand the Packet 7B governed fixture to a real two-provider quorum.
# ---------------------------------------------------------------------------
path = ROOT / "tests" / "test_stage7_review_execution.py"
text = path.read_text(encoding="utf-8")
text = text.replace('provider_id="claude",', 'provider_id="glm",', 1)
text = text.replace('"provider_id": "claude",', '"provider_id": "glm",', 1)
text = text.replace('provider_health={"version": "test", "executable": "claude"},', 'provider_health={"version": "test", "executable": "glm"},', 1)
text = replace_once(
    text,
    '''        self.store.set_provider_constitution_ack(
            "codex",
            {
                "constitution_supported": True,
                "constitution_acknowledged": True,
                "constitution_version": engine.version(),
                "constitution_hash": engine.content_hash(),
                "constitution_last_refresh": "now",
                "constitution_acknowledged_at": "now",
                "constitution_ack_actor": "test",
            },
        )
''',
    '''        for provider_id in ("codex", "claude"):
            self.store.set_provider_constitution_ack(
                provider_id,
                {
                    "constitution_supported": True,
                    "constitution_acknowledged": True,
                    "constitution_version": engine.version(),
                    "constitution_hash": engine.content_hash(),
                    "constitution_last_refresh": "now",
                    "constitution_acknowledged_at": "now",
                    "constitution_ack_actor": "test",
                },
            )
''',
    label="two reviewer acknowledgements",
)
text = replace_once(
    text,
    '''                {"reviewer_id": "codex-reviewer", "provider_id": "codex", "role": "correctness"},
                {"reviewer_id": "grok-reviewer", "provider_id": "grok", "role": "security"},
''',
    '''                {"reviewer_id": "codex-reviewer", "provider_id": "codex", "role": "correctness"},
                {"reviewer_id": "claude-reviewer", "provider_id": "claude", "role": "security"},
''',
    label="two provider assignments",
)
text = replace_once(
    text,
    '''        self.assignment = next(a for a in result["assignments"] if a["provider_id"] == "codex")
''',
    '''        self.assignment = next(a for a in result["assignments"] if a["provider_id"] == "codex")
        self.claude_assignment = next(
            a for a in result["assignments"] if a["provider_id"] == "claude"
        )
''',
    label="Claude assignment fixture",
)
text = replace_once(
    text,
    '''    def _health(self, executable):
        return {
            "provider_id": "codex",
            "live_ready": True,
            "available": True,
            "executable": executable,
            "version": "codex-test",
            "unsupported_reasons": [],
            "auth": {"status": "ready", "probe_verified": True},
        }
''',
    '''    def _health(self, executable):
        return {
            "provider_id": "codex",
            "live_ready": True,
            "available": True,
            "executable": executable,
            "version": "codex-test",
            "unsupported_reasons": [],
            "auth": {"status": "ready", "probe_verified": True},
        }

    def _fake_claude(self, *, payload=None, mutate=False):
        path = Path(self.temp.name) / "claude"
        if payload is None:
            payload = {"verdict": "pass", "summary": "clear", "findings": []}
        wrapper = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": payload,
        }
        path.write_text(
            "#!/usr/bin/env python3\\n"
            "import pathlib, sys\\n"
            "_ = sys.stdin.read()\\n"
            + ("pathlib.Path('claude-wrote.txt').write_text('bad')\\n" if mutate else "")
            + f"print({json.dumps(wrapper)!r})\\n",
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def _claude_health(self, executable):
        return {
            "provider_id": "claude",
            "live_ready": True,
            "available": True,
            "executable": executable,
            "version": "2.1.205",
            "unsupported_reasons": [],
            "auth": {"status": "ready", "probe_verified": True},
        }
''',
    label="Claude fake reviewer fixture",
)
text = replace_once(
    text,
    '''        self.assertEqual(set(REVIEW_COMMAND_CONTRACTS), {"codex"})
        with self.assertRaisesRegex(ValueError, "no approved"):
            build_review_command("claude", "/tmp/claude")
''',
    '''        self.assertEqual(set(REVIEW_COMMAND_CONTRACTS), {"codex", "claude"})
        claude = build_review_command("claude", "/tmp/claude")
        self.assertIn("plan", claude["argv"])
        self.assertIn("Read,Grep,Glob", claude["argv"])
        self.assertNotIn("workspace-write", claude["argv"])
        with self.assertRaisesRegex(ValueError, "no approved"):
            build_review_command("grok", "/tmp/grok")
''',
    label="command contract provider set",
)
anchor = '''    @unittest.skipIf(os.name == "nt", "POSIX executable fixture")
    def test_mutating_reviewer_fails_closed_without_report(self):
'''
new_test = '''    @unittest.skipIf(os.name == "nt", "POSIX executable fixture")
    def test_two_distinct_provider_reviewers_reach_clear_quorum(self):
        codex_executable = self._fake_codex()
        claude_executable = self._fake_claude()

        def health(provider_id, _provider, force_compat=True):
            del force_compat
            if provider_id == "codex":
                return self._health(codex_executable)
            if provider_id == "claude":
                return self._claude_health(claude_executable)
            raise AssertionError(provider_id)

        with patch(
            "buildforme.review_execution.health_check_provider",
            side_effect=health,
        ):
            execute_independent_review_assignment(
                self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
            )
            execute_independent_review_assignment(
                self.store,
                self.cycle["cycle_id"],
                self.claude_assignment["assignment_id"],
            )
        from buildforme.review_service import aggregate_independent_review_cycle

        finalized = aggregate_independent_review_cycle(
            self.store, self.cycle["cycle_id"]
        )
        self.assertEqual(finalized["cycle"]["status"], "clear")
        self.assertEqual(
            set(finalized["aggregate"]["provider_ids"]), {"codex", "claude"}
        )
        self.assertEqual(finalized["aggregate"]["distinct_provider_count"], 2)

'''
if text.count(anchor) != 1:
    raise RuntimeError("two-provider test anchor missing")
text = text.replace(anchor, new_test + anchor, 1)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Dedicated Packet 7C compatibility/parser tests.
# ---------------------------------------------------------------------------
(ROOT / "tests" / "test_stage7_packet7c_claude_reviewer.py").write_text(
    r'''"""Packet 7C contracts for a verified Claude read-only reviewer provider."""

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
''',
    encoding="utf-8",
)

# Permanent source contract.
(ROOT / "tests" / "test_stage7_packet7c_contract.py").write_text(
    '''"""Permanent source contracts for Packet 7C distinct-provider review."""

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
''',
    encoding="utf-8",
)

# Documentation truth.
path = ROOT / "docs" / "STAGE_7_INDEPENDENT_MULTI_AGENT_REVIEW.md"
text = path.read_text(encoding="utf-8")
text += '''

### Packet 7C Claude reviewer contract

- Claude Code authentication is verified through the official machine-readable
  `claude auth status` JSON command; environment markers alone are not accepted.
- Claude Code must be version 2.1.205 or newer and its installed help must expose every
  required noninteractive, JSON Schema, read-only, tool-restriction, MCP-isolation,
  safe-mode, and no-session flag.
- The code-owned review command uses `--permission-mode plan`, limits built-in tools to
  `Read,Grep,Glob`, enables safe mode and strict MCP isolation, disables session
  persistence, and requires validated JSON Schema output.
- Claude output is accepted only from a successful result wrapper containing one
  `structured_output` object. Plain prose, plain report JSON, error subtypes, and missing
  structured output fail closed.
- Codex and Claude can now execute independent blind assignments and satisfy a genuine
  two-provider storage quorum in the integration path. A founder-controlled live smoke
  remains required before Packet 7C acceptance.
'''
path.write_text(text, encoding="utf-8")

print("Stage 7 Packet 7C Claude reviewer capability applied")
