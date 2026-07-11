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
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Stage 6 red-team cancel patch scoped to ProcessSupervisor.cancel")
