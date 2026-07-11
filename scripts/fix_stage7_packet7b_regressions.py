from __future__ import annotations

from pathlib import Path

path = Path(__file__).resolve().parent / "apply_stage7_packet7b.py"
text = path.read_text(encoding="utf-8")

old = '''for rel in ("tests/test_stage6_execution.py", "tests/test_stage7_review_authority.py"):
'''
new = '''for rel in (
    "tests/test_stage6_execution.py",
    "tests/test_stage7_review_authority.py",
    "tests/test_stage7_packet7a_contract.py",
):
'''
if text.count(old) != 1:
    raise RuntimeError(f"schema regression file loop count={text.count(old)}")
text = text.replace(old, new, 1)

runtime_anchor = '''text = text.replace(helper_anchor, helper + helper_anchor, 1)
path.write_text(text, encoding="utf-8")
'''
runtime_replacement = '''text = text.replace(helper_anchor, helper + helper_anchor, 1)
append_only_old = '        with self.assertRaisesRegex(ValueError, "not pending|append-only"):\\n'
append_only_new = (
    '        with self.assertRaisesRegex(\\n'
    '            ValueError, "not pending|append-only|requires pending assignment"\\n'
    '        ):\\n'
)
if text.count(append_only_old) != 1:
    raise RuntimeError(
        f"append-only regression expectation count={text.count(append_only_old)}"
    )
text = text.replace(append_only_old, append_only_new, 1)

divergence_old = \'\'\'        with self.assertRaisesRegex(ValueError, "diverge"):
            self.store.submit_review_report_atomic(
                cycle_id=cycle["cycle_id"],
                assignment_id=assignment["assignment_id"],
                report=report,
                findings=divergent,
                actor="reviewer",
            )
\'\'\'
divergence_new = \'\'\'        with self.assertRaisesRegex(
            ValueError, "direct review report submission disabled"
        ):
            self.store.submit_review_report_atomic(
                cycle_id=cycle["cycle_id"],
                assignment_id=assignment["assignment_id"],
                report=report,
                findings=divergent,
                actor="reviewer",
            )
\'\'\'
if text.count(divergence_old) != 1:
    raise RuntimeError(
        f"legacy divergence expectation count={text.count(divergence_old)}"
    )
text = text.replace(divergence_old, divergence_new, 1)
path.write_text(text, encoding="utf-8")
'''
if text.count(runtime_anchor) != 1:
    raise RuntimeError(f"Packet 7A runtime patch anchor count={text.count(runtime_anchor)}")
text = text.replace(runtime_anchor, runtime_replacement, 1)

old = '''from buildforme.review_execution import (
    REVIEW_COMMAND_CONTRACTS,
    build_review_command,
    build_verified_blind_review_packet,
    execute_independent_review_assignment,
    parse_strict_review_output,
)
from buildforme.review_service import create_independent_review_cycle, submit_independent_review_report
'''
new = '''from buildforme.review_contracts import build_review_report_record
from buildforme.review_execution import (
    REVIEW_COMMAND_CONTRACTS,
    build_review_command,
    build_review_execution_record,
    build_verified_blind_review_packet,
    execute_independent_review_assignment,
    parse_strict_review_output,
)
from buildforme.review_service import create_independent_review_cycle, submit_independent_review_report
'''
if text.count(old) != 1:
    raise RuntimeError(f"Packet 7B test imports count={text.count(old)}")
text = text.replace(old, new, 1)

anchor = '''    def test_direct_report_submission_is_disabled(self):
'''
test = '''    def test_authenticated_storage_rejects_divergent_findings(self):
        packet, snapshot, _root = build_verified_blind_review_packet(
            self.store, self.cycle["cycle_id"], self.assignment["assignment_id"]
        )
        packet = self.store.save_review_packet_atomic(packet=packet, actor="test")
        cycle = self.store.get_review_cycle(self.cycle["cycle_id"])
        assignment = self.store.get_review_assignment(self.assignment["assignment_id"])
        report, findings = build_review_report_record(
            cycle=cycle,
            assignment=assignment,
            payload={
                "verdict": "changes_required",
                "summary": "repair",
                "findings": [
                    {
                        "severity": "medium",
                        "category": "correctness",
                        "summary": "bug",
                        "evidence": "app.py line 1",
                        "recommendation": "repair",
                    }
                ],
            },
        )
        process = {
            "ok": True,
            "exit_code": 0,
            "pid": 123,
            "stdout": "{}",
            "stderr": "",
            "cleanup_ok": True,
            "process_group_isolated": True,
            "argv": ["test-reviewer", "--read-only"],
        }
        execution = build_review_execution_record(
            packet=packet,
            assignment=assignment,
            command={
                "contract_id": "test.read-only.v1",
                "read_only": True,
                "argv": process["argv"],
            },
            health={"version": "test", "executable": "test-reviewer"},
            process_result=process,
            pre_snapshot=snapshot,
            post_snapshot=snapshot,
            status="succeeded",
            report_fingerprint=report["report_fingerprint"],
        )
        divergent = [dict(findings[0])]
        divergent[0]["summary"] = "different row"
        with self.assertRaisesRegex(ValueError, "diverge"):
            self.store.submit_review_report_atomic(
                cycle_id=cycle["cycle_id"],
                assignment_id=assignment["assignment_id"],
                report=report,
                findings=divergent,
                actor="reviewer",
                execution=execution,
            )
        self.assertEqual(self.store.list_review_reports(cycle["cycle_id"]), [])
        self.assertEqual(
            self.store.list_review_execution_attempts(assignment["assignment_id"]), []
        )

'''
if text.count(anchor) != 1:
    raise RuntimeError(f"Packet 7B divergence test anchor count={text.count(anchor)}")
text = text.replace(anchor, test + anchor, 1)

path.write_text(text, encoding="utf-8")
print("Packet 7B regression templates corrected")
