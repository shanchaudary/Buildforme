from __future__ import annotations

from pathlib import Path

path = Path(__file__).resolve().parent / "apply_stage7_packet7a_review_shopping.py"
text = path.read_text(encoding="utf-8")
marker = 'print("Stage 7 Packet 7A review-shopping remediation applied")\n'
if text.count(marker) != 1:
    raise RuntimeError(f"expected one completion marker, found {text.count(marker)}")
injection = r'''# The stronger same-evidence prohibition may fire before the active-cycle check.
path = ROOT / "tests" / "test_stage7_review_authority.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '        with self.assertRaisesRegex(ValueError, "active independent review cycle"):\n',
    '        with self.assertRaisesRegex(ValueError, "active independent review cycle|already been independently reviewed"):\n',
    label="active cycle regression expectation",
)
path.write_text(text, encoding="utf-8")

'''
path.write_text(text.replace(marker, injection + marker, 1), encoding="utf-8")
print("Review-shopping patcher regression expectation corrected")
