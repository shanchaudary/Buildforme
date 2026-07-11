from __future__ import annotations

from pathlib import Path

path = Path(__file__).resolve().parent / "apply_stage6_redteam_round2.py"
text = path.read_text(encoding="utf-8")

old = '''text = replace_once(
    text,
    \'\'\'                force_wait_sec=FORCE_WAIT_SEC,
            )
\'\'\',
    \'\'\'                force_wait_sec=FORCE_WAIT_SEC,
                windows_job=windows_job,
            )
\'\'\',
    label="cancel passes Windows job",
)
'''
new = '''cancel_start = text.index("    def cancel(self, run_id: str)")
cancel_prefix = text[:cancel_start]
cancel_text = text[cancel_start:]
cancel_text = replace_once(
    cancel_text,
    \'\'\'                force_wait_sec=FORCE_WAIT_SEC,
            )
\'\'\',
    \'\'\'                force_wait_sec=FORCE_WAIT_SEC,
                windows_job=windows_job,
            )
\'\'\',
    label="cancel passes Windows job",
)
text = cancel_prefix + cancel_text
'''
if text.count(old) != 1:
    raise RuntimeError(f"expected one generic cancel patch block, found {text.count(old)}")
text = text.replace(old, new, 1)

# The replacement template is itself a Python triple-quoted string. Preserve a
# backslash-n in the generated provider_discovery.py instead of embedding a real
# newline inside the target string literal.
old_auth = '        combined = stdout + "\\n" + stderr\n'
new_auth = '        combined = stdout + "\\\\n" + stderr\n'
if text.count(old_auth) != 1:
    raise RuntimeError(f"expected one auth newline template, found {text.count(old_auth)}")
text = text.replace(old_auth, new_auth, 1)

path.write_text(text, encoding="utf-8")
print("Stage 6 red-team patcher corrections applied")
