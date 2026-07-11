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
mutation_end = text.index("    def admit_run_atomic(", mutation_start)
mutation_prefix = text[:mutation_start]
mutation_text = text[mutation_start:mutation_end]
mutation_suffix = text[mutation_end:]
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
mutation_text = replace_once(
    mutation_text,
    \'\'\'            if cur.rowcount == 0:
'''
if text.count(old_tail) != 1:
    raise RuntimeError(f"expected one mutation insert anchor, found {text.count(old_tail)}")
text = text.replace(old_tail, new_tail, 1)

old_finish = '''    label="atomic outcome evidence insert",
)

# Atomic migration wrapper: rename existing importer and add temp-db cutover authority.
'''
new_finish = '''    label="atomic outcome evidence insert",
)
text = mutation_prefix + mutation_text + mutation_suffix

# Atomic migration wrapper: rename existing importer and add temp-db cutover authority.
'''
if text.count(old_finish) != 1:
    raise RuntimeError(f"expected one mutation method finish anchor, found {text.count(old_finish)}")
text = text.replace(old_finish, new_finish, 1)

path.write_text(text, encoding="utf-8")
print("Stage 6 final-blocker patch anchors scoped to commit_run_mutation")
