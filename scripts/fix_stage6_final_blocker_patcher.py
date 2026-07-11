from __future__ import annotations

from pathlib import Path

path = Path(__file__).resolve().parent / "apply_stage6_final_blockers.py"
text = path.read_text(encoding="utf-8")
old = '''text = replace_once(
    text,
    \'\'\'            new_ver = current_ver + 1
            record["row_version"] = new_ver
            cur = conn.execute(
\'\'\',
'''
new = '''mutation_start = text.index("    def commit_run_mutation(")
mutation_prefix = text[:mutation_start]
mutation_text = text[mutation_start:]
mutation_text = replace_once(
    mutation_text,
    \'\'\'            new_ver = current_ver + 1
            record["row_version"] = new_ver
            cur = conn.execute(
\'\'\',
'''
if text.count(old) != 1:
    raise RuntimeError(f"expected one generic mutation anchor, found {text.count(old)}")
text = text.replace(old, new, 1)
old_tail = '''    label="atomic outcome evidence validation",
)
text = replace_once(
    text,
    \'\'\'            if cur.rowcount == 0:
'''
new_tail = '''    label="atomic outcome evidence validation",
)
text = mutation_prefix + mutation_text
text = replace_once(
    text,
    \'\'\'            if cur.rowcount == 0:
'''
if text.count(old_tail) != 1:
    raise RuntimeError(f"expected one mutation anchor tail, found {text.count(old_tail)}")
text = text.replace(old_tail, new_tail, 1)
path.write_text(text, encoding="utf-8")
print("Stage 6 final-blocker patch anchor narrowed")
