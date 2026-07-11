from pathlib import Path

path = Path(__file__).resolve().parent.parent / "tests" / "test_stage7_packet7d_repair_authority.py"
text = path.read_text(encoding="utf-8")
old = '''        reports = []
        findings = []
'''
new = '''        reports = []
        findings = []
        report_by_assignment = {}
'''
if text.count(old) != 1:
    raise RuntimeError("report fixture anchor mismatch")
text = text.replace(old, new, 1)
old = '''            reports.append(report)
            findings.extend(report_findings)
'''
new = '''            reports.append(report)
            report_by_assignment[assignment["assignment_id"]] = report["report_id"]
            findings.extend(report_findings)
'''
if text.count(old) != 1:
    raise RuntimeError("report map anchor mismatch")
text = text.replace(old, new, 1)
old = '''                    (finding["finding_id"], finding["report_id"], self.cycle["cycle_id"], finding["assignment_id"], finding["severity"], finding["category"], 1 if finding["blocking"] else 0, finding["finding_fingerprint"], dumps(finding), "now"),
'''
new = '''                    (finding["finding_id"], report_by_assignment[finding["assignment_id"]], self.cycle["cycle_id"], finding["assignment_id"], finding["severity"], finding["category"], 1 if finding["blocking"] else 0, finding["finding_fingerprint"], dumps(finding), "now"),
'''
if text.count(old) != 1:
    raise RuntimeError("finding foreign-key anchor mismatch")
text = text.replace(old, new, 1)
path.write_text(text, encoding="utf-8")
print("Packet 7D fixture corrected")
