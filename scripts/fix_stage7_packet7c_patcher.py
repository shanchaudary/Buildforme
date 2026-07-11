from __future__ import annotations

from pathlib import Path

path = Path(__file__).resolve().parent / "apply_stage7_packet7c.py"
text = path.read_text(encoding="utf-8")

old = '''text = replace_once(
    text,
    \'\'\'    "codex": {
        "args": ["login", "status"],
        "success_exit_codes": [0],
        "positive_patterns": [r"(?i)logged in"],
        "failure_patterns": [r"(?i)not logged in", r"(?i)logged out"],
        "contract_source": "openai/codex codex-rs/cli/src/login.rs run_login_status",
        "read_only": True,
    },
\'\'\',
    \'\'\'    "codex": {
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
        "positive_patterns": [r\'"loggedIn"\\\\s*:\\\\s*true\'],
        "failure_patterns": [r\'"loggedIn"\\\\s*:\\\\s*false\'],
        "contract_source": "Claude Code CLI reference: claude auth status JSON",
        "read_only": True,
    },
\'\'\',
    label="Claude auth probe contract",
)
'''
new = '''text = replace_once(
    text,
    \'\'\'    # No primary-source, machine-verifiable status contract has been accepted yet.
    "claude": None,
\'\'\',
    \'\'\'    "claude": {
        "args": ["auth", "status"],
        "success_exit_codes": [0],
        "success_patterns": [r\'"loggedIn"\\\\s*:\\\\s*true\'],
        "failure_patterns": [r\'"loggedIn"\\\\s*:\\\\s*false\'],
        "read_only": True,
        "contract_source": "Claude Code CLI reference: claude auth status JSON",
    },
\'\'\',
    label="Claude auth probe contract",
)
'''
if text.count(old) != 1:
    raise RuntimeError(f"Packet 7C auth patch block count={text.count(old)}")
text = text.replace(old, new, 1)

marker = 'print("Stage 7 Packet 7C Claude reviewer capability applied")\n'
addition = '''# ---------------------------------------------------------------------------
# Fix Packet 7B successful assignment transition exposed by two-provider quorum.
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "execution_store.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    \'\'\'            conn.execute(
                "UPDATE review_assignments SET status=\'submitted\', payload_json=?, submitted_at=? WHERE id=? AND status=\'pending\'",
                (dumps(assignment), now, assignment_id),
            )
\'\'\',
    \'\'\'            assignment_cur = conn.execute(
                "UPDATE review_assignments SET status=\'submitted\', payload_json=?, submitted_at=? WHERE id=? AND status=\'executing\'",
                (dumps(assignment), now, assignment_id),
            )
            if assignment_cur.rowcount != 1:
                raise ValueError("review assignment submission race rejected")
\'\'\',
    label="successful reviewer assignment transition",
)
path.write_text(text, encoding="utf-8")

# Claude now has an accepted machine-readable auth contract. A non-Claude executable
# invoked with `auth status` fails rather than remaining unknown.
path = ROOT / "tests" / "test_stage6_redteam_round2.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    \'\'\'    def test_unverified_claude_status_contract_fails_closed(self):
        result = probe_authentication("claude", sys.executable)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["probe_verified"])
\'\'\',
    \'\'\'    def test_claude_auth_probe_fails_closed_on_non_claude_executable(self):
        result = probe_authentication("claude", sys.executable)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["probe_verified"])
\'\'\',
    label="Claude auth regression",
)
path.write_text(text, encoding="utf-8")

'''
if text.count(marker) != 1:
    raise RuntimeError(f"Packet 7C final marker count={text.count(marker)}")
text = text.replace(marker, addition + marker, 1)

path.write_text(text, encoding="utf-8")
print("Packet 7C patcher corrections applied")
