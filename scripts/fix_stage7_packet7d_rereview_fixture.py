from pathlib import Path

path = Path(__file__).resolve().parent.parent / "tests" / "test_stage7_packet7d_repair_rereview.py"
text = path.read_text(encoding="utf-8")
old = 'fixture = Stage7RepairAdmissionTests(methodName="test_schema_v8")'
new = 'fixture = Stage7RepairAdmissionTests(methodName="test_schema_v7")'
if text.count(old) != 1:
    raise RuntimeError("Packet 7D re-review fixture anchor mismatch")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Packet 7D re-review fixture corrected")
