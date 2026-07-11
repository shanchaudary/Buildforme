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
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Packet 7C auth probe patch anchor corrected")
