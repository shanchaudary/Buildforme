from __future__ import annotations

from pathlib import Path

path = Path(__file__).resolve().parent / "apply_stage7_packet7a.py"
text = path.read_text(encoding="utf-8")
needle = 'print("Stage 7 Packet 7A review authority applied")\n'
if text.count(needle) != 1:
    raise RuntimeError(f"expected one Packet 7A completion marker, found {text.count(needle)}")
injection = r'''# Stage 7 advances the execution authority schema from v3 to v4.
path = ROOT / "tests" / "test_stage6_execution.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '        self.assertEqual(p["schema_version"], 3)\n',
    '        self.assertEqual(p["schema_version"], 4)\n',
    label="Stage 6 schema expectation after Stage 7 migration",
)
path.write_text(text, encoding="utf-8")

'''
path.write_text(text.replace(needle, injection + needle, 1), encoding="utf-8")
print("Stage 7 Packet 7A patcher corrected for schema v4 regression")
